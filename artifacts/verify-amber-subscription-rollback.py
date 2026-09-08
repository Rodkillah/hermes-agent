#!/usr/bin/env python3
"""Exercise the bounded Amber subscription rollback on a disposable DB only."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "profile-overlay/amber/scripts/kanban_telegram_subscribe_all.py"
os.environ["HERMES_AGENT_RUNTIME"] = str(ROOT)
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("amber_subscription", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
kb = module.kb


def seed(conn, owner=None, mode=None, chat_id="chat"):
    task_id = kb.create_task(conn, title="rollback fixture", assignee="worker")
    if owner is not None:
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id=chat_id,
            thread_id="thread",
            user_id="user",
            user_id_alt="alt",
            chat_type="dm",
            notifier_profile=owner,
            delivery_mode=mode or "notify+wake",
            delivery_metadata={"fixture": "rollback", "thread": 7},
        )
    return task_id


def unread(conn, task_id):
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'changes_requested', '{}', 123)",
            (task_id,),
        )
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = 0 WHERE task_id = ?",
            (task_id,),
        )


with tempfile.TemporaryDirectory(prefix="amber-subscription-rollback-") as temp:
    db = Path(temp) / "kanban.db"
    kb.init_db(db)
    conn = kb.connect(db)
    try:
        anchor = seed(conn, owner="amber")
        forge = seed(conn, owner="forge")
        notify_only = seed(conn, owner="amber", mode="notify")
        created = seed(conn)
        unread(conn, forge)
        unread(conn, notify_only)
        unread(conn, created)
        human = seed(conn, owner="human", chat_id="human-chat")
        before_forge = dict(kb.list_notify_subs(conn, forge)[0])
        before_notify = dict(kb.list_notify_subs(conn, notify_only)[0])
        backup = Path(temp) / "before-rollback.db"
        conn.execute("VACUUM INTO ?", (str(backup),))
        with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as backup_conn:
            assert backup_conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert backup_conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 5
            assert backup_conn.execute("SELECT count(*) FROM kanban_notify_subs").fetchone()[0] == 4

        migrated = module.reconcile(conn)
        key = dict(task_id=forge, platform="telegram", chat_id="chat", thread_id="thread")
        _, advanced_cursor, claimed = kb.claim_unseen_events_for_sub(conn, **key)
        assert claimed and advanced_cursor > before_forge["last_event_id"]

        # Independent post-migration data must survive the targeted rollback.
        independent = seed(conn, owner="human", chat_id="independent-chat")
        independent_before = dict(kb.list_notify_subs(conn, independent)[0])

        assert kb.transfer_notify_sub_owner(
            conn, task_id=forge, platform="telegram", chat_id="chat", thread_id="thread",
            expected_owner="amber", new_owner="forge",
        )
        kb.add_notify_sub(
            conn, task_id=notify_only, platform="telegram", chat_id="chat", thread_id="thread",
            delivery_mode="notify",
        )
        assert kb.remove_notify_sub(
            conn, task_id=created, platform="telegram", chat_id="chat", thread_id="thread",
        )

        assert kb.list_notify_subs(conn, forge)[0]["notifier_profile"] == "forge"
        assert kb.list_notify_subs(conn, forge)[0]["last_event_id"] == advanced_cursor
        assert kb.list_notify_subs(conn, notify_only)[0]["delivery_mode"] == before_notify["delivery_mode"]
        assert kb.list_notify_subs(conn, created) == []
        assert dict(kb.list_notify_subs(conn, independent)[0]) == independent_before
        assert not kb.claim_unseen_events_for_sub(conn, **key, kinds=["changes_requested"])[2]

        reapplied = module.reconcile(conn)
        stable = module.reconcile(conn)
        rows = kb.list_notify_subs(conn)
        assert reapplied["changed"] == 4, reapplied
        assert stable["changed"] == 0, stable
        assert sum(row["task_id"] == created for row in rows) == 1
        assert kb.list_notify_subs(conn, forge)[0]["last_event_id"] == advanced_cursor
        assert dict(kb.list_notify_subs(conn, independent)[0]) == independent_before
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()

print(json.dumps({
    "migration": migrated,
    "claimed_before_rollback": len(claimed),
    "advanced_cursor_preserved": advanced_cursor,
    "rollback": "native owner inverse + mode restore + created-row remove",
    "independent_data_preserved": True,
    "reapply_changed": reapplied["changed"],
    "second_pass_changed": stable["changed"],
    "quick_check": "ok",
}, sort_keys=True))
print("targeted Amber rollback verifier: PASS")
