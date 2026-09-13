from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_v2_schema_init_is_additive_and_idempotent(kanban_home):
    kb.init_db()
    with kbc.connect_closing() as conn:
        tables = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"kanban_notify_subs", "kanban_notify_routes", "kanban_notify_authorities"} <= tables
        route_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(kanban_notify_routes)")
        }
        authority_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(kanban_notify_authorities)")
        }
    assert {
        "route_id", "ping_subscription_id", "last_ping_event_id",
        "claim_event_id", "claim_token", "claim_expires_at",
    } <= route_columns
    assert {
        "subscription_id", "legacy_subscription_id", "bot_profile",
        "notifier_profile", "delivery_mode", "ping_priority", "source_kind",
        "last_wake_event_id", "claim_event_id", "claim_token", "claim_expires_at",
    } <= authority_columns


def test_two_authorities_share_one_route_and_unique_priority_elects_ping(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="shared route")
        amber_id = kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="private-route",
            bot_profile="amber",
            notifier_profile="amber",
            delivery_mode="notify+wake",
            ping_priority=0,
            source_kind="inherited",
        )
        forge_id = kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="private-route",
            bot_profile="forge",
            notifier_profile="forge",
            delivery_mode="notify+wake",
            ping_priority=100,
            source_kind="default",
        )
        routes = kbn.list_notify_routes(conn, task_id)
        authorities = kbn.list_notify_authorities(conn, task_id)

    assert len(routes) == 1
    assert len(authorities) == 2
    assert {a["subscription_id"] for a in authorities} == {amber_id, forge_id}
    assert routes[0]["ping_subscription_id"] == forge_id


def test_legacy_mapping_is_atomic_complete_and_idempotent(kanban_home):
    with kbc.connect_closing() as conn:
        legacy = []
        for index in range(11):
            owner = "amber" if index < 6 else "forge"
            task_id = kb.create_task(conn, title=f"legacy-{index}")
            kbn.add_notify_sub(
                conn,
                task_id=task_id,
                platform="telegram",
                chat_id=f"route-{index}",
                notifier_profile=owner,
                delivery_mode="notify+wake",
            )
            sub = kbn.list_notify_subs(conn, task_id)[0]
            legacy.append({
                "subscription_id": sub["subscription_id"],
                "bot_profile": owner,
            })

        incomplete = kbn.migrate_notify_authorities(
            conn, mappings=legacy[:-1], apply=True
        )
        assert incomplete == {
            "mappable": 10, "unmapped": 1, "ambiguous": 0,
            "already_migrated": 0, "applied": 0,
        }
        assert kbn.list_notify_authorities(conn) == []

        ambiguous = kbn.migrate_notify_authorities(
            conn, mappings=[*legacy, legacy[0]], apply=True
        )
        assert ambiguous["ambiguous"] == 1
        assert ambiguous["applied"] == 0
        assert kbn.list_notify_authorities(conn) == []

        applied = kbn.migrate_notify_authorities(conn, mappings=legacy, apply=True)
        assert applied == {
            "mappable": 11, "unmapped": 0, "ambiguous": 0,
            "already_migrated": 0, "applied": 11,
        }
        leases = {a["subscription_id"] for a in kbn.list_notify_authorities(conn)}
        assert len(leases) == 11
        assert kbn.list_notify_subs(conn) == []

        replay = kbn.migrate_notify_authorities(conn, mappings=legacy, apply=True)
        assert replay == {
            "mappable": 0, "unmapped": 0, "ambiguous": 0,
            "already_migrated": 11, "applied": 0,
        }
        assert {a["subscription_id"] for a in kbn.list_notify_authorities(conn)} == leases


def test_creation_inherits_authority_then_applies_higher_priority_default(kanban_home):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        kbn.add_notify_authority(
            conn,
            task_id=parent,
            platform="telegram",
            chat_id="physical-room",
            bot_profile="forge",
            notifier_profile="amber",
            delivery_mode="notify+wake",
            ping_priority=10,
        )
        (kanban_home / "config.yaml").write_text(
            """kanban:
  default_notify_targets:
    - board: default
      platform: telegram
      chat_id: physical-room
      bot_profile: forge
      notifier_profile: forge
      delivery_mode: notify+wake
      ping_priority: 100
      source_kind: default
""",
            encoding="utf-8",
        )

        child = kb.create_task(conn, title="child", parents=[parent])
        authorities = kbn.list_notify_authorities(conn, child)
        routes = kbn.list_notify_routes(conn, child)

    assert len(routes) == 1
    assert {(a["bot_profile"], a["notifier_profile"]) for a in authorities} == {
        ("forge", "amber"),
        ("forge", "forge"),
    }
    elected = next(a for a in authorities if a["notifier_profile"] == "forge")
    assert routes[0]["ping_subscription_id"] == elected["subscription_id"]
    assert {a["source_kind"] for a in authorities} == {"inherited", "default"}


def test_creator_authority_is_copied_as_inherited(kanban_home):
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="creator")
        kbn.add_notify_authority(
            conn,
            task_id=creator,
            platform="telegram",
            chat_id="room",
            bot_profile="forge",
            notifier_profile="forge",
            delivery_mode="notify+wake",
            source_kind="creator",
        )
        child = kb.create_task(conn, title="child", creator_task_id=creator)
        authority = kbn.list_notify_authorities(conn, child)[0]
    assert authority["source_kind"] == "inherited"


def test_authority_mutation_rotates_leases_and_empty_route_is_collected(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="leases")
        forge = kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="room",
            bot_profile="forge",
            notifier_profile="forge",
            delivery_mode="notify+wake",
            ping_priority=100,
        )
        amber = kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="room",
            bot_profile="amber",
            notifier_profile="amber",
            delivery_mode="notify+wake",
            ping_priority=10,
        )
        route_1 = kbn.list_notify_routes(conn, task_id)[0]

        assert kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="room",
            bot_profile="forge",
            notifier_profile="forge",
            delivery_mode="notify+wake",
            ping_priority=100,
        ) == forge
        assert kbn.list_notify_routes(conn, task_id)[0]["route_id"] == route_1["route_id"]

        amber_2 = kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="room",
            bot_profile="amber",
            notifier_profile="amber",
            delivery_mode="notify+wake",
            ping_priority=200,
        )
        route_2 = kbn.list_notify_routes(conn, task_id)[0]
        assert amber_2 != amber
        assert route_2["route_id"] != route_1["route_id"]
        assert route_2["ping_subscription_id"] == amber_2

        assert kbn.remove_notify_authority(conn, subscription_id=amber_2)
        route_3 = kbn.list_notify_routes(conn, task_id)[0]
        assert route_3["route_id"] != route_2["route_id"]
        assert route_3["ping_subscription_id"] == forge
        assert kbn.remove_notify_authority(conn, subscription_id=forge)
        assert kbn.list_notify_routes(conn, task_id) == []


def test_ping_and_each_runtime_claim_same_event_independently(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="claims")
        for owner, priority in (("forge", 100), ("amber", 10)):
            kbn.add_notify_authority(
                conn,
                task_id=task_id,
                platform="telegram",
                chat_id="room",
                bot_profile=owner,
                notifier_profile=owner,
                delivery_mode="notify+wake",
                ping_priority=priority,
            )
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "review_requested", {"reviewer": "architect"})
        route = kbn.list_notify_routes(conn, task_id)[0]
        authorities = kbn.list_notify_authorities(conn, task_id)

        ping = kbn.claim_notify_ping(conn, route_id=route["route_id"], now=100)
        wakes = [
            kbn.claim_notify_wake(conn, subscription_id=a["subscription_id"], now=100)
            for a in authorities
        ]

        assert ping is not None
        assert all(wake is not None for wake in wakes)
        assert {wake["event"]["id"] for wake in wakes} == {ping["event"]["id"]}
        assert ping["bot_profile"] == "forge"
        assert {wake["notifier_profile"] for wake in wakes} == {"amber", "forge"}

        assert kbn.settle_notify_ping(
            conn,
            route_id=ping["route_id"],
            claim_token=ping["claim_token"],
            event_id=ping["event"]["id"],
        )
        for wake in wakes:
            assert kbn.settle_notify_wake(
                conn,
                subscription_id=wake["subscription_id"],
                claim_token=wake["claim_token"],
                event_id=wake["event"]["id"],
            )
        assert kbn.claim_notify_ping(conn, route_id=route["route_id"], now=101) is None
        assert all(
            kbn.claim_notify_wake(conn, subscription_id=a["subscription_id"], now=101)
            is None
            for a in authorities
        )


def test_gc_removes_stale_v2_authorities_routes_and_projection(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="stale", assignee="worker")
        authority_id = kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="room",
            bot_profile="forge",
            notifier_profile="forge",
            delivery_mode="notify+wake",
        )
        kbn.project_notify_authority_to_legacy(
            conn, subscription_id=authority_id
        )
        assert kb.complete_task(conn, task_id, summary="done")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = 0 WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE tasks SET completed_at = 0, created_at = 0 WHERE id = ?",
                (task_id,),
            )

        assert kbn.purge_stale_done_notify_subs(conn, max_age_days=1) == 1
        assert kbn.list_notify_authorities(conn, task_id) == []
        assert kbn.list_notify_routes(conn, task_id) == []
        assert conn.execute(
            "SELECT 1 FROM kanban_notify_subs WHERE task_id = ?", (task_id,)
        ).fetchone() is None


def test_expired_ping_claim_is_reclaimed_and_stale_token_is_fenced(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="claim expiry")
        kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="room",
            bot_profile="forge",
            notifier_profile="amber",
            delivery_mode="notify",
            source_kind="manual",
        )
        route = kbn.list_notify_routes(conn, task_id)[0]
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "completed", {})

        first = kbn.claim_notify_ping(
            conn, route_id=route["route_id"], now=100, lease_seconds=10
        )
        assert first is not None
        assert kbn.claim_notify_ping(
            conn, route_id=route["route_id"], now=109, lease_seconds=10
        ) is None
        replacement = kbn.claim_notify_ping(
            conn, route_id=route["route_id"], now=110, lease_seconds=10
        )
        assert replacement is not None
        assert replacement["event"]["id"] == first["event"]["id"]
        assert replacement["claim_token"] != first["claim_token"]
        assert not kbn.settle_notify_ping(
            conn,
            route_id=first["route_id"],
            claim_token=first["claim_token"],
            event_id=first["event"]["id"],
        )
        assert kbn.settle_notify_ping(
            conn,
            route_id=replacement["route_id"],
            claim_token=replacement["claim_token"],
            event_id=replacement["event"]["id"],
        )


def test_manual_subscribe_cli_creates_v2_authority(kanban_home, capsys):
    from hermes_cli import kanban as cli

    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="manual")
    args = argparse.Namespace(
        task_id=task_id,
        platform="telegram",
        chat_id="room",
        thread_id=None,
        chat_type="group",
        user_id=None,
        user_id_alt=None,
        notifier_profile="amber",
        bot_profile="forge",
        delivery_mode="notify+wake",
        ping_priority=50,
    )

    assert cli._cmd_notify_subscribe(args) == 0
    assert "room" not in capsys.readouterr().out
    with kbc.connect_closing() as conn:
        authority = kbn.list_notify_authorities(conn, task_id)[0]
    assert authority["bot_profile"] == "forge"
    assert authority["notifier_profile"] == "amber"
    assert authority["ping_priority"] == 50
    assert authority["source_kind"] == "manual"


def test_legacy_projection_roundtrip_preserves_v2_and_catches_up_monotonically(
    kanban_home,
):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="rollback projection")
        subscription_id = kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="room",
            thread_id="topic",
            bot_profile="forge",
            notifier_profile="forge",
            delivery_mode="notify+wake",
            source_kind="manual",
        )
        with kb.write_txn(conn):
            for _ in range(2):
                kb._append_event(conn, task_id, "completed", {})
        route = kbn.list_notify_routes(conn, task_id)[0]
        ping = kbn.claim_notify_ping(conn, route_id=route["route_id"])
        wake = kbn.claim_notify_wake(conn, subscription_id=subscription_id)
        assert ping is not None and wake is not None
        assert kbn.settle_notify_ping(
            conn, route_id=route["route_id"], claim_token=ping["claim_token"],
            event_id=ping["event"]["id"],
        )
        assert kbn.settle_notify_wake(
            conn, subscription_id=subscription_id, claim_token=wake["claim_token"],
            event_id=wake["event"]["id"],
        )

        legacy_id = kbn.project_notify_authority_to_legacy(
            conn, subscription_id=subscription_id
        )
        legacy = kbn.list_notify_subs(conn, include_linked=True)[0]
        assert legacy["subscription_id"] == legacy_id
        assert legacy["last_ping_event_id"] == ping["event"]["id"]
        assert legacy["last_event_id"] == wake["event"]["id"]
        v2_before = (
            kbn.list_notify_routes(conn, task_id)[0],
            kbn.list_notify_authorities(conn, task_id)[0],
        )

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_notify_subs SET last_ping_event_id = ?, last_event_id = ? "
                "WHERE subscription_id = ?",
                (ping["event"]["id"] + 10, wake["event"]["id"] + 9, legacy_id),
            )
        assert (
            kbn.list_notify_routes(conn, task_id)[0],
            kbn.list_notify_authorities(conn, task_id)[0],
        ) == v2_before

        assert kbn.reconcile_notify_authority_from_legacy(
            conn, subscription_id=subscription_id
        )
        route_after = kbn.list_notify_routes(conn, task_id)[0]
        authority_after = kbn.list_notify_authorities(conn, task_id)[0]
        assert route_after["last_ping_event_id"] == ping["event"]["id"] + 10
        assert authority_after["last_wake_event_id"] == wake["event"]["id"] + 9


def test_ping_priority_tie_and_duplicate_wake_runtime_fail_before_write(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="ambiguous authority")
        elected = kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="room",
            bot_profile="forge",
            notifier_profile="forge",
            delivery_mode="notify+wake",
            ping_priority=100,
        )

        with pytest.raises(ValueError, match="ambiguous ping priority"):
            kbn.add_notify_authority(
                conn,
                task_id=task_id,
                platform="telegram",
                chat_id="room",
                bot_profile="amber",
                notifier_profile="amber",
                delivery_mode="notify",
                ping_priority=100,
            )
        assert [a["subscription_id"] for a in kbn.list_notify_authorities(conn, task_id)] == [
            elected
        ]
        assert kbn.list_notify_routes(conn, task_id)[0]["ping_subscription_id"] == elected

        notify_only = kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="room",
            bot_profile="amber",
            notifier_profile="forge",
            delivery_mode="notify",
            ping_priority=10,
        )
        with pytest.raises(ValueError, match="wake runtime already has an authority"):
            kbn.add_notify_authority(
                conn,
                task_id=task_id,
                platform="telegram",
                chat_id="room",
                bot_profile="other",
                notifier_profile="forge",
                delivery_mode="wake",
            )
        assert {a["subscription_id"] for a in kbn.list_notify_authorities(conn, task_id)} == {
            elected,
            notify_only,
        }


def test_expired_claim_is_not_current_before_another_worker_reclaims(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="claim expiry fence")
        kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="room",
            bot_profile="forge",
            notifier_profile="forge",
            delivery_mode="notify",
        )
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "completed", {})
        route = kbn.list_notify_routes(conn, task_id)[0]
        claim = kbn.claim_notify_ping(
            conn, route_id=route["route_id"], now=100, lease_seconds=10
        )
        assert claim is not None
        kwargs = {
            "flow": "ping",
            "lease_id": claim["route_id"],
            "claim_token": claim["claim_token"],
            "event_id": claim["event"]["id"],
        }
        assert kbn.notify_v2_claim_is_current(conn, **kwargs, now=109)
        assert not kbn.notify_v2_claim_is_current(conn, **kwargs, now=110)


def test_migration_command_accepts_normative_board_option_position(tmp_path):
    from hermes_cli import kanban_parser as parser_module

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    parser_module.build_parser(subparsers)
    mapping = str(tmp_path / "mapping.json")

    after_action = parser.parse_args([
        "kanban", "notify-migrate-authorities", "--board", "iron-rod",
        "--mapping-file", mapping, "--dry-run", "--json",
    ])
    before_action = parser.parse_args([
        "kanban", "--board", "iron-rod", "notify-migrate-authorities",
        "--mapping-file", mapping, "--dry-run", "--json",
    ])

    assert after_action.board == before_action.board == "iron-rod"
    assert after_action.kanban_action == before_action.kanban_action == (
        "notify-migrate-authorities"
    )


def test_hard_delete_removes_v2_authority_and_route_rows(kanban_home):
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="delete v2 task")
        kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="private-route",
            bot_profile="forge",
            notifier_profile="forge",
        )
        assert kb.delete_task(conn, task_id) is True
        assert kbn.list_notify_authorities(conn, task_id) == []
        assert kbn.list_notify_routes(conn, task_id) == []


def test_notify_list_and_unsubscribe_address_v2_authority(kanban_home, capsys):
    from hermes_cli import kanban as cli

    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="operator lifecycle")
        kbn.add_notify_authority(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="room",
            bot_profile="forge",
            notifier_profile="amber",
            delivery_mode="notify+wake",
            ping_priority=50,
        )

    list_args = argparse.Namespace(task_id=task_id, json=True)
    assert cli._cmd_notify_list(list_args) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    assert listed[0]["schema_version"] == 2
    assert listed[0]["bot_profile"] == "forge"
    assert listed[0]["notifier_profile"] == "amber"

    unsubscribe_args = argparse.Namespace(
        task_id=task_id,
        platform="telegram",
        chat_id="room",
        thread_id=None,
        bot_profile="forge",
        notifier_profile="amber",
    )
    assert cli._cmd_notify_unsubscribe(unsubscribe_args) == 0
    capsys.readouterr()
    with kbc.connect_closing() as conn:
        assert kbn.list_notify_authorities(conn, task_id) == []
        assert kbn.list_notify_routes(conn, task_id) == []


def test_migration_cli_requires_private_mapping_and_prints_counts_only(
    kanban_home, tmp_path, capsys
):
    from hermes_cli import kanban as cli

    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="cli migration")
        kbn.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="private-route",
            notifier_profile="amber",
            delivery_mode="notify+wake",
        )
        legacy_id = kbn.list_notify_subs(conn, task_id)[0]["subscription_id"]

    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps([{"subscription_id": legacy_id, "bot_profile": "amber"}]),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        mapping_file=str(mapping), dry_run=False, apply=True, json=True
    )

    mapping.chmod(0o644)
    assert cli._cmd_notify_migrate_authorities(args) == 1
    assert legacy_id not in capsys.readouterr().err

    mapping.chmod(0o600)
    args.dry_run, args.apply = True, False
    assert cli._cmd_notify_migrate_authorities(args) == 0
    dry_output = json.loads(capsys.readouterr().out)
    assert dry_output == {
        "mappable": 1,
        "unmapped": 0,
        "ambiguous": 0,
        "already_migrated": 0,
    }

    args.dry_run, args.apply = False, True
    assert cli._cmd_notify_migrate_authorities(args) == 0
    applied_output = json.loads(capsys.readouterr().out)
    assert applied_output == {
        "mappable": 1,
        "unmapped": 0,
        "ambiguous": 0,
        "already_migrated": 0,
    }
    with kbc.connect_closing() as conn:
        assert len(kbn.list_notify_authorities(conn, task_id)) == 1
