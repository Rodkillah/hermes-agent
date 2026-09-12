"""Shared, deterministic policy for Kanban goal completion and review readiness."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from agent.redact import redact_sensitive_text

POLICY_VERSION = 1
_VALID_VERDICTS = frozenset({"done", "continue", "wait", "blocked"})
_SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
_CREDENTIAL_REMOTE = re.compile(
    r"(?:^[a-z][a-z0-9+.-]*://|@|\bbearer\b|\btoken\b|\bpassword\b)", re.IGNORECASE,
)
_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9_:-]{1,200}$")
ATTEMPT_RESERVATION_TTL_SECONDS = 120
ATTEMPT_JUDGE_TIMEOUT_SECONDS = 90
_ATTEMPT_RESERVED_KIND = "goal_gate_attempt_reserved"
_ATTEMPT_RESULT_KIND = "goal_gate_attempt_result"
_PERSISTED_RESULTS = frozenset({
    ("allow", "goal_gate_done", None),
    ("reject", "goal_gate_continue", None),
    ("reject", "goal_gate_wait", None),
    ("reject", "goal_gate_blocked", None),
    ("allow_with_diagnostic", "goal_gate_unavailable", "unavailable"),
    ("allow_with_diagnostic", "goal_gate_unavailable", "transport"),
    ("allow_with_diagnostic", "goal_gate_unavailable", "parse"),
})


@dataclass(frozen=True)
class GateDecision:
    outcome: str
    code: str
    classification: Optional[str] = None
    message: str = ""


class GoalGateAuditUnavailable(RuntimeError):
    """The fail-open decision could not be durably audited."""

    code = "goal_gate_audit_unavailable"


def _safe_text(value: Any) -> str:
    return redact_sensitive_text(
        str(value or ""), force=True, redact_url_credentials=True,
    ).strip()


def completion_decision(
    verdict: Any,
    reason: Any,
    transport_failed: bool,
    parse_failed: bool,
) -> GateDecision:
    """Classify one completed judge call; transport flags take precedence."""
    if transport_failed:
        return GateDecision(
            "allow_with_diagnostic", "goal_gate_unavailable", "transport",
            "Goal judge transport was unavailable; completion may proceed with an audit event.",
        )
    if parse_failed or verdict not in _VALID_VERDICTS:
        return GateDecision(
            "allow_with_diagnostic", "goal_gate_unavailable", "parse",
            "Goal judge output was unusable; completion may proceed with an audit event.",
        )
    if verdict == "done":
        return GateDecision("allow", "goal_gate_done")
    safe_reason = _safe_text(reason) or "no reason supplied"
    if verdict == "blocked":
        message = (
            "Goal completion rejected: judge ruled the goal unachievable — "
            f"{safe_reason}. Re-scope the task or record the external block with "
            "kanban_block (`kanban block` on the CLI)."
        )
    elif verdict == "wait":
        message = (
            f"Goal completion rejected by judge (wait): {safe_reason}. "
            "Keep the task open until the stated condition is satisfied."
        )
    else:
        message = (
            f"Goal completion rejected by judge: {safe_reason}. "
            "Provide evidence matching the task's acceptance criteria."
        )
    return GateDecision("reject", f"goal_gate_{verdict}", None, message)


def unavailable_decision() -> GateDecision:
    return GateDecision(
        "allow_with_diagnostic", "goal_gate_unavailable", "unavailable",
        "Goal judge could not be resolved; completion may proceed with an audit event.",
    )


def run_completion_gate(
    conn, task, evidence: str, *, run_id: Optional[int], judge,
    attempt_metadata: Optional[dict] = None,
) -> GateDecision:
    """Run the final-completion policy once and persist any required override audit."""
    if task is None or not task.goal_mode:
        return GateDecision("allow", "goal_gate_not_applicable")
    if task.status not in {"running", "ready", "blocked", "review"}:
        return GateDecision(
            "reject", "goal_gate_transition_ineligible", None,
            "Task is not in a completable state.",
        )
    if run_id is not None and task.current_run_id != run_id:
        return GateDecision(
            "reject", "goal_gate_transition_ineligible", None,
            "Worker run is stale; completion was not evaluated.",
        )

    try:
        attempt_id = new_attempt_id(task.id, run_id, evidence, attempt_metadata)
    except Exception as exc:
        raise GoalGateAuditUnavailable(
            "goal_gate_audit_unavailable: completion attempt identity could not be derived"
        ) from exc
    try:
        reserved = _reserve_attempt(conn, task.id, run_id, attempt_id)
    except Exception as exc:
        raise GoalGateAuditUnavailable(
            "goal_gate_audit_unavailable: completion attempt could not be durably reserved"
        ) from exc
    if not reserved:
        try:
            decision = _load_attempt_result(conn, task.id, attempt_id)
            if decision is None:
                lease_expires_at = _reservation_lease_expires_at(conn, task.id, attempt_id)
                if lease_expires_at is None:
                    raise RuntimeError("goal gate reservation is unreadable")
                if time.time() >= lease_expires_at:
                    decision = unavailable_decision()
                    decision = _persist_or_load_result(
                        conn, task.id, run_id, attempt_id, decision,
                    )
                else:
                    return GateDecision(
                        "reject", "goal_gate_attempt_in_progress", None,
                        "This completion attempt is already being evaluated; retry shortly.",
                    )
            return _audit_if_required(
                conn, task.id, run_id, attempt_id, decision, None, None,
            )
        except GoalGateAuditUnavailable:
            raise
        except Exception as exc:
            raise GoalGateAuditUnavailable(
                "goal_gate_audit_unavailable: completion attempt state could not be durably resolved"
            ) from exc

    client = model = None
    try:
        from agent.auxiliary_client import get_text_auxiliary_client

        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        decision = unavailable_decision()
    else:
        if client is None or not model:
            decision = unavailable_decision()
        else:
            try:
                verdict, reason, parse_failed, _, transport_failed = judge(
                    goal=f"{task.title}\n\n{task.body or ''}".strip(),
                    last_response=evidence.strip(),
                    timeout=ATTEMPT_JUDGE_TIMEOUT_SECONDS,
                )
            except Exception:
                decision = GateDecision(
                    "allow_with_diagnostic", "goal_gate_unavailable", "transport",
                    "Goal judge transport was unavailable; completion may proceed with an audit event.",
                )
            else:
                decision = completion_decision(
                    verdict, reason, bool(transport_failed), bool(parse_failed),
                )
    try:
        decision = _persist_or_load_result(conn, task.id, run_id, attempt_id, decision)
    except Exception as exc:
        raise GoalGateAuditUnavailable(
            "goal_gate_audit_unavailable: completion decision could not be durably recorded"
        ) from exc
    return _audit_if_required(conn, task.id, run_id, attempt_id, decision, client, model)


def _audit_if_required(
    conn, task_id: str, run_id: Optional[int], attempt_id: str,
    decision: GateDecision, client: Any, model: Any,
) -> GateDecision:
    if decision.outcome != "allow_with_diagnostic":
        return decision
    try:
        record_unavailable_audit(
            conn, task_id=task_id, run_id=run_id, attempt_id=attempt_id,
            classification=decision.classification or "parse", client=client, model=model,
        )
    except Exception as exc:
        raise GoalGateAuditUnavailable(
            "goal_gate_audit_unavailable: the goal judge was unavailable, but the "
            "fail-open override could not be durably audited; completion was not attempted"
        ) from exc
    return decision


def _attempt_payload(conn, task_id: str, kind: str, attempt_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "AND json_extract(payload, '$.attempt_id') = ? ORDER BY id LIMIT 1",
        (task_id, kind, attempt_id),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _reserve_attempt(conn, task_id: str, run_id: Optional[int], attempt_id: str) -> bool:
    from hermes_cli import kanban_db as kb

    reserved_at = time.time()
    return kb.append_idempotent_event(
        conn, task_id, _ATTEMPT_RESERVED_KIND,
        {
            "attempt_id": attempt_id,
            "reserved_at": reserved_at,
            "lease_expires_at": reserved_at + ATTEMPT_RESERVATION_TTL_SECONDS,
            "policy_version": POLICY_VERSION,
        },
        idempotency_field="attempt_id", run_id=run_id,
    )


def _reservation_lease_expires_at(conn, task_id: str, attempt_id: str) -> Optional[float]:
    payload = _attempt_payload(conn, task_id, _ATTEMPT_RESERVED_KIND, attempt_id)
    try:
        if payload is None:
            return None
        if "lease_expires_at" in payload:
            return float(payload["lease_expires_at"])
        # Candidate reservations written before the bounded lease field existed.
        return float(payload["reserved_at"]) + ATTEMPT_RESERVATION_TTL_SECONDS
    except (KeyError, TypeError, ValueError):
        return None


def _load_attempt_result(conn, task_id: str, attempt_id: str) -> Optional[GateDecision]:
    payload = _attempt_payload(conn, task_id, _ATTEMPT_RESULT_KIND, attempt_id)
    if payload is None:
        return None
    outcome = payload.get("outcome")
    code = payload.get("code")
    classification = payload.get("classification")
    if not isinstance(outcome, str) or not isinstance(code, str):
        return None
    if classification is not None and not isinstance(classification, str):
        return None
    if (outcome, code, classification) not in _PERSISTED_RESULTS:
        return None
    return GateDecision(outcome, code, classification, _persisted_message(code))


def _persisted_message(code: str) -> str:
    """Rebuild actionable UX without persisting free-form judge text."""
    if code == "goal_gate_blocked":
        return (
            "Goal completion rejected: judge ruled the goal unachievable. "
            "Re-scope the task or record the external block with kanban_block "
            "(`kanban block` on the CLI)."
        )
    if code == "goal_gate_wait":
        return "Goal completion rejected by judge (wait). Keep the task open and retry later."
    if code == "goal_gate_continue":
        return "Goal completion rejected by judge. Provide evidence matching the acceptance criteria."
    if code == "goal_gate_unavailable":
        return "Goal judge could not be resolved; completion may proceed with an audit event."
    return ""


def _persist_or_load_result(
    conn, task_id: str, run_id: Optional[int], attempt_id: str, decision: GateDecision,
) -> GateDecision:
    from hermes_cli import kanban_db as kb

    result_key = (decision.outcome, decision.code, decision.classification)
    if result_key not in _PERSISTED_RESULTS:
        raise ValueError("invalid goal gate attempt result")
    inserted = kb.append_idempotent_event(
        conn, task_id, _ATTEMPT_RESULT_KIND,
        {
            "attempt_id": attempt_id,
            "outcome": decision.outcome,
            "code": decision.code,
            "classification": decision.classification,
            "policy_version": POLICY_VERSION,
        },
        idempotency_field="attempt_id", run_id=run_id,
    )
    if inserted:
        return decision
    persisted = _load_attempt_result(conn, task_id, attempt_id)
    if persisted is None:
        raise RuntimeError("goal gate attempt result is unreadable")
    return persisted


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_pairs(value: Any, left: str, right: str) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, dict) and _nonempty(item.get(left)) and _nonempty(item.get(right))
                for item in value)
    )


def review_readiness_decision(metadata: Any) -> GateDecision:
    """Validate metadata.review_readiness V1 without consulting an LLM."""
    readiness = metadata.get("review_readiness") if isinstance(metadata, dict) else None
    if not isinstance(readiness, dict):
        return _readiness_reject(["metadata.review_readiness"])

    errors: list[str] = []
    if readiness.get("schema_version") != 1:
        errors.append("schema_version")
    kind = readiness.get("candidate_kind")
    if kind not in {"git", "artifact"}:
        errors.append("candidate_kind")
        return _readiness_reject(errors)

    if kind == "git":
        candidate = readiness.get("candidate_sha")
        base = readiness.get("base_sha")
        if not isinstance(candidate, str) or not _SHA40.fullmatch(candidate):
            errors.append("candidate_sha")
        if (not isinstance(base, str) or not _SHA40.fullmatch(base)
                or (isinstance(candidate, str) and base.lower() == candidate.lower())):
            errors.append("base_sha")
        remote = readiness.get("remote")
        if not _nonempty(remote) or _CREDENTIAL_REMOTE.search(remote):
            errors.append("remote")
        if not _nonempty(readiness.get("remote_ref")):
            errors.append("remote_ref")
        changed = readiness.get("changed_files")
        if not isinstance(changed, list) or any(
            not _nonempty(path) or path.startswith("/") for path in changed
        ):
            errors.append("changed_files")
        elif not changed and not _nonempty(readiness.get("no_diff_reason")):
            errors.append("no_diff_reason")
        if not _valid_pairs(readiness.get("tests_run"), "command", "result"):
            errors.append("tests_run")
    else:
        if not _nonempty(readiness.get("candidate_identity")):
            errors.append("candidate_identity")
        if not _nonempty(readiness.get("candidate_location")):
            errors.append("candidate_location")
        if not _valid_pairs(readiness.get("verification"), "check", "result"):
            errors.append("verification")

    if not (_nonempty(readiness.get("rollback"))
            or _nonempty(readiness.get("rollback_not_applicable_reason"))):
        errors.append("rollback")
    if not isinstance(readiness.get("limits"), list):
        errors.append("limits")
    return _readiness_reject(errors) if errors else GateDecision("allow", "review_readiness_ready")


def _readiness_reject(fields: list[str]) -> GateDecision:
    return GateDecision(
        "reject", "review_readiness_invalid", None,
        "Invalid metadata.review_readiness V1 fields: " + ", ".join(fields),
    )


def new_attempt_id(
    task_id: str,
    run_id: Optional[int],
    evidence: str = "",
    metadata: Optional[dict] = None,
) -> str:
    """Derive a stable, non-reversible key for one logical completion attempt.

    Replays and concurrent calls carrying the same run and proof deduplicate, while
    an explicit retry with corrected evidence or metadata receives a new identity.
    Only the digest is persisted; raw handoff material never enters the audit event.
    """
    logical_metadata = (
        {key: value for key, value in metadata.items() if key != "worker_session_id"}
        if isinstance(metadata, dict) else metadata
    )
    proof = json.dumps(
        {"evidence": evidence, "metadata": logical_metadata},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(proof).hexdigest()
    identity = hashlib.sha256(
        f"{task_id}\0{run_id if run_id is not None else 'none'}\0{digest}".encode("utf-8")
    ).hexdigest()
    return f"gga:v{POLICY_VERSION}:{identity}"


def record_unavailable_audit(
    conn,
    *,
    task_id: str,
    run_id: Optional[int],
    attempt_id: str,
    classification: str,
    client: Any,
    model: Any,
) -> bool:
    """Atomically append one redacted audit event for an attempt id."""
    if classification not in {"unavailable", "transport", "parse"}:
        raise ValueError("invalid goal gate audit classification")
    if not isinstance(attempt_id, str) or not _ATTEMPT_ID.fullmatch(attempt_id):
        raise ValueError("invalid goal gate attempt id")
    from hermes_cli import kanban_db as kb

    payload = {
        "classification": classification,
        "policy_version": POLICY_VERSION,
        "attempt_id": attempt_id,
    }
    return kb.append_idempotent_event(
        conn,
        task_id,
        "goal_gate_unavailable",
        payload,
        idempotency_field="attempt_id",
        run_id=run_id,
    )
