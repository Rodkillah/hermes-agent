"""1Password Login items as a vault backend (``op`` CLI).

Unlock: ``op signin --raw`` with the master password on stdin (desktop-app
integration or account-level auth) mints an ``OP_SESSION_<account>`` token.
A configured service-account token skips the prompt entirely (headless).
List: ``op item list --categories Login --format json`` → title, urls,
username. Resolve: ``op item get <id> --fields label=password --reveal``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set

from agent.secret_sources.base import run_cli
from agent.secret_sources.onepassword import _OP_ENV_ALLOWLIST, _scrub, find_op
from agent.vault_backends.base import LoginBackend, UnlockRequired, run_with_stdin_secret
from agent.vault_backends import unlock as _unlock
from agent.vault_store import VaultItemMeta, normalize_origin

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


class OnePasswordLoginBackend(LoginBackend):
    name = "onepassword"
    display_name = "1Password"
    prefix = "op:"
    needs_unlock = True

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}
        from agent.secret_scope import get_secret
        env_name = str(self.cfg.get("service_account_token_env") or "OP_SERVICE_ACCOUNT_TOKEN")
        self._service_token = get_secret(env_name, "") or ""
        # In-memory item_id -> set of distinct vault identities, built ONLY from
        # the non-sensitive ``item.vault`` metadata returned by ``list_items`` and
        # republished atomically on every successful list. The public handle stays
        # ``op:<item_id>`` (no vault identity in it), so this index is what lets
        # ``resolve_password`` / ``resolve_otp`` add ``--vault`` at fill time. A
        # single identity is resolvable; zero or several are not (fail closed).
        self._vault_index: Dict[str, Set[str]] = {}

    # ── auth ────────────────────────────────────────────────────────────────

    def _op(self) -> Path:
        op = find_op(str(self.cfg.get("binary_path") or ""))
        if op is None:
            raise RuntimeError("1Password CLI (op) not found — install it or set vault.onepassword.binary_path")
        return op

    def _env(self, session_token: Optional[str]) -> Dict[str, str]:
        from agent.secret_scope import get_secret
        env = {k: os.environ[k] for k in _OP_ENV_ALLOWLIST if k in os.environ and not k.startswith("OP_CONNECT_")}
        # Connect credentials outrank OP_SERVICE_ACCOUNT_TOKEN inside op, so they must come from the
        # profile's own secret scope like the service token does — never from the launch environment.
        for k in ("OP_CONNECT_HOST", "OP_CONNECT_TOKEN"):
            if v := get_secret(k, ""):
                env[k] = v
        env["NO_COLOR"] = "1"
        account = str(self.cfg.get("account") or "")
        if account:
            env["OP_ACCOUNT"] = account
        if self._service_token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = self._service_token
        elif session_token:
            # op signin --raw prints the bare token; the env var name carries the account shorthand,
            # which op also accepts as plain OP_SESSION for the default account.
            env[f"OP_SESSION_{account}" if account else "OP_SESSION"] = session_token
        return env

    def is_unlocked(self) -> bool:
        return bool(self._service_token) or _unlock.is_unlocked(self.name)

    def unlock(self, master_password: str) -> None:
        """Mint a session token from the master password (consumed on stdin, never argv)."""
        generation = _unlock.begin_unlock(self.name)
        cmd = [str(self._op()), "signin", "--raw"]
        if account := str(self.cfg.get("account") or ""):
            cmd += ["--account", account]
        proc = run_with_stdin_secret(cmd, env=self._env(None), secret=master_password, timeout=_TIMEOUT, label="op")
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            raise RuntimeError(f"1Password unlock failed: {_scrub(proc.stderr or '')[:200] or 'no session token'}")
        if not _unlock.store_session_token(self.name, token, generation):
            raise RuntimeError("1Password was locked while unlocking; try again")

    def _run(self, *args: str) -> str:
        token = None if self._service_token else _unlock.get_session_token(self.name)
        if not self._service_token and not token:
            raise UnlockRequired(self)
        proc = run_cli([str(self._op()), *args], env=self._env(token), timeout=_TIMEOUT, label="op",
                       timeout_message="op timed out", stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            err = _scrub(proc.stderr or "")
            if "session" in err.lower() or "sign in" in err.lower() or "not signed in" in err.lower():
                _unlock.lock(self.name)
                raise UnlockRequired(self)
            raise RuntimeError(f"op failed: {err[:200]}")
        return proc.stdout or ""

    # ── backend contract ───────────────────────────────────────────────────
    def list_items(self) -> List[VaultItemMeta]:
        if not self.is_unlocked():
            return []
        raw = json.loads(self._run("item", "list", "--categories", "Login", "--format", "json") or "[]")
        # Rebuild the index from scratch on every successful list, then publish it
        # atomically. This drops any stale identity whose current metadata no longer
        # carries a vault, and aggregates every distinct identity an item_id appears
        # under (no last-wins). The public handle list is built from the same pass.
        fresh_index: Dict[str, Set[str]] = {}
        out: List[VaultItemMeta] = []
        for item in raw if isinstance(raw, list) else []:
            item_id = str(item.get("id") or "")
            vault = self._vault_identity(item.get("vault"))
            if item_id and vault:
                fresh_index.setdefault(item_id, set()).add(vault)
            urls = [str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")]
            origins = _all_origins(urls)
            if not origins:
                continue
            username = str(item.get("additional_information") or "").strip() or None
            out.append(VaultItemMeta(
                id=f"{self.prefix}{item.get('id')}", kind="login", label=str(item.get("title") or origins[0]),
                origin=origins[0], created_at=str(item.get("created_at") or ""),
                identifier_type="username" if username else None, identifier=username,
                allowed_origins=_web_origins(origins)))
        self._vault_index = fresh_index
        return out

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        return next((m for m in self.list_items() if m.id == handle), None)

    @staticmethod
    def _vault_identity(vault: object) -> Optional[str]:
        """Extract a stable vault identity from ``item.vault`` metadata.

        Accepts the realistic shapes ``{"id": ..., "name": ...}`` (preferring the
        stable ``id``) or a bare string. Returns None when no identity is present.
        Never hardcodes a vault name or id.
        """
        if isinstance(vault, dict):
            vid = str(vault.get("id") or "").strip()
            if vid:
                return vid
            name = str(vault.get("name") or "").strip()
            return name or None
        if isinstance(vault, str):
            return vault.strip() or None
        return None

    def _resolve_vault(self, item_id: str) -> str:
        """Return the single vault identity for ``item_id``, recovering it via a
        bounded ``item list`` metadata refresh when the in-memory index has no
        entry (an old handle resolved before any ``list_items``). Fails closed
        with a non-secret error when the identity is absent or ambiguous — never
        an unbounded ``item get`` without ``--vault`` under a service account.
        """
        vault = self._single_vault(item_id)
        if vault:
            return vault
        # Preserve the original UnlockRequired contract: a locked backend must
        # surface as locked, not as "no vault metadata" (list_items returns []
        # while locked, which would otherwise mask the real cause).
        if not self.is_unlocked():
            raise UnlockRequired(self)
        # Bounded, non-revealing metadata recovery: item list only, never item get.
        self.list_items()
        vault = self._single_vault(item_id)
        if not vault:
            raise RuntimeError(
                f"1Password item {item_id!r} has no unambiguous vault metadata; "
                "cannot resolve it under a service account (a vault query is "
                "required). Re-run browser_vault_list to refresh the item's vault, "
                "or grant the item a single vault."
            )
        return vault

    def _single_vault(self, item_id: str) -> Optional[str]:
        """The unique vault identity for ``item_id``, or None when zero or several
        distinct identities are known (both are non-resolvable and must fail closed).
        """
        identities = self._vault_index.get(item_id)
        if not identities:
            return None
        if len(identities) == 1:
            return next(iter(identities))
        return None

    def resolve_password(self, handle: str) -> str:
        item_id = handle[len(self.prefix):]
        vault = self._resolve_vault(item_id)
        return self._run("item", "get", item_id, "--vault", vault,
                          "--fields", "label=password", "--reveal").rstrip("\r\n")

    def resolve_otp(self, handle: str) -> Optional[str]:
        # `--otp` mints the current TOTP from the item's one-time-password field; items without one error out.
        item_id = handle[len(self.prefix):]
        vault = self._resolve_vault(item_id)
        try:
            code = self._run("item", "get", item_id, "--vault", vault, "--otp").strip()
        except Exception:
            return None
        return code if code.isdigit() else None


def _web_origins(origins: List[str]) -> tuple:
    """Fill targets are browser pages, so app URIs (``androidapp://`` etc.) never
    widen the fill set; an item whose only URI is an app URI keeps its single
    (unfillable-from-a-page) origin exactly as before."""
    web = tuple(o for o in origins if o.startswith(("http://", "https://")))
    return web or (origins[0],)


def _all_origins(urls: List[str]) -> List[str]:
    """Every normalized origin saved on the item, deduped, order preserved.

    A 1Password Login item can carry several websites; each of them is a place the
    user told 1Password the credential belongs, so all of them are valid fill targets.
    """
    out: List[str] = []
    for u in urls:
        try:
            origin = normalize_origin(u)
        except Exception:
            continue
        if origin not in out:
            out.append(origin)
    return out
