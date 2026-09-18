"""Persisted notification routes authorize exactly one transport, including route-only profiles."""
import asyncio
from pathlib import Path

from gateway.config import GatewayConfig, Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.profile_routing import parse_profile_routes
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn


class RecordingAdapter:
    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


def setup_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    for name in ("yuki", "other"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: RecordingAdapter()}
    runner._profile_adapters = {"yuki": {}}
    runner._primary_profile_name = "default"
    runner._kanban_notifier_profile = "default"
    runner._kanban_dispatcher_lock_handle = object()
    runner.config = GatewayConfig(multiplex_profiles=True, profile_routes=parse_profile_routes([
        dict(platform="discord", guild_id="guild", chat_id="parent", profile="yuki"),
    ]))
    return runner


def completion(*, profile: str | None = "yuki", metadata=None, chat="post", thread="post", mode="notify+wake"):
    with kbc.connect() as conn:
        task = kb.create_task(conn, title="route completion", assignee="worker")
        kbn.add_notify_sub(conn, task_id=task, platform="discord", chat_id=chat,
                           thread_id=thread, chat_type="thread", user_id="creator",
                           notifier_profile=profile, delivery_mode=mode,
                           delivery_metadata=metadata if metadata is not None else
                           {"guild_id": "guild", "scope_id": "guild", "parent_chat_id": "parent"})
        kb.complete_task(conn, task, result="finished")
    return task


def collect(runner):
    return _notifier_collect(runner, kb, notifier_profile="default", gc_due=False, gc_retention_days=30)


async def deliver(runner, rows):
    for row in rows:
        await _KanbanNotification(runner, row, platform_cls=Platform, sub_fail_counts={}).deliver()


def unseen(task):
    with kbc.connect() as conn:
        return kbn.unseen_events_for_sub(conn, task_id=task, platform="discord", chat_id="post",
                                         thread_id="post", kinds=["completed"])[1]


def test_two_authorities_emit_one_ping_and_two_independent_wakes(tmp_path, monkeypatch):
    runner = setup_runner(tmp_path, monkeypatch)
    primary = runner.adapters[Platform.DISCORD]
    secondary = RecordingAdapter()
    runner._profile_adapters["other"] = {Platform.DISCORD: secondary}  # type: ignore[dict-item]
    metadata = {"guild_id": "guild", "scope_id": "guild", "parent_chat_id": "parent"}
    with kbc.connect() as conn:
        task = kb.create_task(conn, title="v2 completion", assignee="worker")
        kbn.add_notify_authority(
            conn,
            task_id=task,
            platform="discord",
            chat_id="post",
            thread_id="post",
            chat_type="thread",
            user_id="creator",
            bot_profile="default",
            notifier_profile="yuki",
            delivery_mode="notify+wake",
            delivery_metadata=metadata,
            ping_priority=100,
        )
        kbn.add_notify_authority(
            conn,
            task_id=task,
            platform="discord",
            chat_id="post",
            thread_id="post",
            chat_type="thread",
            user_id="creator",
            bot_profile="other",
            notifier_profile="other",
            delivery_mode="notify+wake",
            delivery_metadata=metadata,
            ping_priority=10,
        )
        kb.complete_task(conn, task, result="finished")

    rows = collect(runner)
    assert [(row["sub"]["flow"], row["sub"]["notifier_profile"]) for row in rows] == [
        ("ping", "yuki"),
        ("wake", "other"),
        ("wake", "yuki"),
    ]
    asyncio.run(deliver(runner, rows))

    assert len(getattr(primary, "sent")) == 1
    assert len(getattr(primary, "handled")) == 1
    assert secondary.sent == []
    assert len(secondary.handled) == 1
    assert not collect(runner)


def test_ping_authority_is_owned_by_bot_not_wake_runtime(tmp_path, monkeypatch):
    runner = setup_runner(tmp_path, monkeypatch)
    primary = runner.adapters[Platform.DISCORD]
    with kbc.connect() as conn:
        task = kb.create_task(conn, title="transport-only authority", assignee="worker")
        kbn.add_notify_authority(
            conn,
            task_id=task,
            platform="discord",
            chat_id="post",
            thread_id="post",
            chat_type="thread",
            bot_profile="default",
            notifier_profile="offline-runtime",
            delivery_mode="notify",
            ping_priority=100,
        )
        kb.complete_task(conn, task, result="finished")

    rows = collect(runner)
    assert [(row["sub"]["flow"], row["sub"]["bot_profile"]) for row in rows] == [
        ("ping", "default")
    ]
    asyncio.run(deliver(runner, rows))
    assert len(primary.sent) == 1
    assert primary.handled == []
    assert not collect(runner)


def test_exact_routed_profile_delivers_once_on_its_authorized_transport(tmp_path, monkeypatch):
    runner = setup_runner(tmp_path, monkeypatch)
    primary = runner.adapters[Platform.DISCORD]
    task = completion(metadata={"scope_id": "guild", "guild_id": "stale-alias", "parent_chat_id": "parent"})
    rows = collect(runner)
    assert [row["task"].id for row in rows] == [task]
    asyncio.run(deliver(runner, rows))
    assert len(primary.sent) == len(primary.handled) == 1
    source = primary.handled[0].source
    assert (source.profile, source.guild_id, source.scope_id, source.parent_chat_id) == (
        "yuki", "guild", "guild", "parent")
    assert runner._adapter_for_source(source) is primary
    assert not collect(runner)

    # A connected secondary owns its credential even where the primary route matches.
    secondary = RecordingAdapter()
    secondary.scope_id_for_chat = lambda chat: "stale-cache"
    runner._profile_adapters["yuki"] = {Platform.DISCORD: secondary}
    task = completion(metadata={"guild_id": "guild", "parent_chat_id": "parent"})
    asyncio.run(deliver(runner, collect(runner)))
    assert len(primary.sent) == 1
    assert len(secondary.sent) == len(secondary.handled) == 1
    assert secondary.handled[0].source.scope_id == "guild"
    assert runner._adapter_for_source(secondary.handled[0].source) is secondary
    assert not unseen(task)


def test_rebound_subscription_rejects_the_stale_claim(tmp_path, monkeypatch):
    runner = setup_runner(tmp_path, monkeypatch)
    primary = runner.adapters[Platform.DISCORD]
    task = completion(profile="yuki", mode="notify")
    rows = collect(runner)
    assert len(rows) == 1
    stale_sub = rows[0]["sub"]

    with kbc.connect() as conn:
        assert kbn.remove_notify_sub(
            conn, task_id=task, platform="discord", chat_id="post",
            thread_id="post",
        )
        kbn.add_notify_sub(
            conn, task_id=task, platform="discord", chat_id="post",
            thread_id="post", chat_type="thread", notifier_profile="other",
            delivery_mode="notify",
            delivery_metadata={
                "guild_id": "guild", "scope_id": "guild",
                "parent_chat_id": "parent",
            },
        )
        rebound = kbn.list_notify_subs(conn, task)[0]
        assert rebound["subscription_id"] != stale_sub["subscription_id"]
        rebound_cursor = rebound["last_event_id"]

    asyncio.run(deliver(runner, rows))

    assert getattr(primary, "sent") == []
    with kbc.connect() as conn:
        current = kbn.list_notify_subs(conn, task)[0]
    assert current["subscription_id"] == rebound["subscription_id"]
    assert current["last_event_id"] == rebound_cursor


def test_identical_resubscribe_preserves_claim_and_delivers_once(tmp_path, monkeypatch):
    runner = setup_runner(tmp_path, monkeypatch)
    primary = runner.adapters[Platform.DISCORD]
    task = completion()
    rows = collect(runner)
    assert len(rows) == 1
    claimed_sub = rows[0]["sub"]

    with kbc.connect() as conn:
        kbn.add_notify_sub(
            conn, task_id=task, platform="discord", chat_id="post",
            thread_id="post", user_id="creator", chat_type="thread",
            notifier_profile="yuki", delivery_mode="notify+wake",
            delivery_metadata={
                "guild_id": "guild", "scope_id": "guild",
                "parent_chat_id": "parent",
            },
        )
        current = kbn.list_notify_subs(conn, task)[0]
    assert current["subscription_id"] == claimed_sub["subscription_id"]

    asyncio.run(deliver(runner, rows))

    assert len(primary.sent) == len(primary.handled) == 1
    assert not unseen(task)
    assert not collect(runner)


def test_unowned_routed_subscription_delivers_once_without_dispatch_lock(tmp_path, monkeypatch):
    runner = setup_runner(tmp_path, monkeypatch)
    runner._kanban_dispatcher_lock_handle = None
    primary = runner.adapters[Platform.DISCORD]
    foreign = RecordingAdapter()
    runner._profile_adapters["other"] = {Platform.DISCORD: foreign}  # type: ignore[dict-item]
    task = completion(profile=None, mode="notify")

    rows = collect(runner)
    assert [row["task"].id for row in rows] == [task]
    asyncio.run(deliver(runner, rows))

    assert len(getattr(primary, "sent")) == 1
    assert foreign.sent == []
    assert not unseen(task)
    assert not collect(runner)


def test_ownerless_route_uses_declared_bot_profile_transport(tmp_path, monkeypatch):
    runner = setup_runner(tmp_path, monkeypatch)
    primary = runner.adapters[Platform.DISCORD]
    other = RecordingAdapter()
    runner._profile_adapters["other"] = {Platform.DISCORD: other}  # type: ignore[dict-item]
    runner.config.profile_routes = parse_profile_routes([
        dict(
            platform="discord", guild_id="guild", chat_id="parent",
            profile="yuki", bot_profile="other",
        ),
    ])
    task = completion(profile=None, mode="notify")

    rows = collect(runner)
    assert [row["task"].id for row in rows] == [task]
    asyncio.run(deliver(runner, rows))

    assert getattr(primary, "sent") == []
    assert len(other.sent) == 1
    assert not unseen(task)


def test_ownerless_wake_uses_routed_profile_and_runtime_scope(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home

    runner = setup_runner(tmp_path, monkeypatch)
    home = tmp_path / ".hermes"
    observed = []

    class ScopedAdapter(RecordingAdapter):
        async def handle_message(self, event):
            await asyncio.sleep(0)
            observed.append((event.source.profile, get_hermes_home()))
            await super().handle_message(event)

    primary = ScopedAdapter()
    runner.adapters[Platform.DISCORD] = primary  # type: ignore[assignment]
    task = completion(profile=None)

    rows = collect(runner)
    assert [row["task"].id for row in rows] == [task]
    asyncio.run(deliver(runner, rows))

    assert len(primary.sent) == len(primary.handled) == 1
    assert observed == [("yuki", home / "profiles" / "yuki")]
    assert not unseen(task)
    assert not collect(runner)


def test_removed_runtime_never_loads_global_scope_or_delivers(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    import shutil

    runner = setup_runner(tmp_path, monkeypatch)
    primary = runner.adapters[Platform.DISCORD]
    task = completion(profile=None)
    rows = collect(runner)
    assert len(rows) == 1
    yuki_home = tmp_path / ".hermes" / "profiles" / "yuki"
    original_resolver = runner._resolve_profile_home_for_source
    entered_homes = []

    def remove_then_resolve(source):
        shutil.rmtree(yuki_home)
        return original_resolver(source)

    @asynccontextmanager
    async def record_scope(profile_home):
        entered_homes.append(profile_home)
        yield

    runner._resolve_profile_home_for_source = remove_then_resolve
    monkeypatch.setattr("gateway.run._async_profile_runtime_scope", record_scope)
    asyncio.run(deliver(runner, rows))

    assert entered_homes == []
    assert getattr(primary, "sent") == []
    assert getattr(primary, "handled") == []
    assert unseen(task)


def test_removed_bot_profile_cannot_use_its_stale_adapter(tmp_path, monkeypatch):
    import shutil

    runner = setup_runner(tmp_path, monkeypatch)
    primary = runner.adapters[Platform.DISCORD]
    other = RecordingAdapter()
    runner._profile_adapters["other"] = {Platform.DISCORD: other}  # type: ignore[dict-item]
    runner.config.profile_routes = parse_profile_routes([
        dict(
            platform="discord", guild_id="guild", chat_id="parent",
            profile="yuki", bot_profile="other",
        ),
    ])
    task = completion(profile=None, mode="notify")
    rows = collect(runner)
    assert len(rows) == 1
    shutil.rmtree(tmp_path / ".hermes" / "profiles" / "other")

    asyncio.run(deliver(runner, rows))

    assert getattr(primary, "sent") == []
    assert other.sent == []
    assert unseen(task)


def test_route_mutation_after_ping_blocks_stale_wake(tmp_path, monkeypatch):
    runner = setup_runner(tmp_path, monkeypatch)

    class MutatingAdapter(RecordingAdapter):
        async def send(self, chat_id, text, **kwargs):
            await super().send(chat_id, text, **kwargs)
            runner.config.profile_routes = parse_profile_routes([
                dict(
                    platform="discord", guild_id="guild", chat_id="parent",
                    profile="other",
                ),
            ])

    adapter = MutatingAdapter()
    runner.adapters[Platform.DISCORD] = adapter  # type: ignore[assignment]
    task = completion(profile=None)
    rows = collect(runner)
    assert len(rows) == 1

    asyncio.run(deliver(runner, rows))

    assert len(adapter.sent) == 1
    assert adapter.handled == []
    assert unseen(task)


def test_route_mutation_while_runtime_scope_loads_is_retryable(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    runner = setup_runner(tmp_path, monkeypatch)
    primary = runner.adapters[Platform.DISCORD]
    task = completion(profile=None)
    rows = collect(runner)
    assert len(rows) == 1

    @asynccontextmanager
    async def mutate_route(_profile_home):
        runner.config.profile_routes = parse_profile_routes([
            dict(
                platform="discord", guild_id="guild", chat_id="parent",
                profile="other",
            ),
        ])
        yield

    monkeypatch.setattr("gateway.run._async_profile_runtime_scope", mutate_route)
    asyncio.run(deliver(runner, rows))

    assert getattr(primary, "sent") == []
    assert getattr(primary, "handled") == []
    assert unseen(task)


def test_route_denials_leave_events_retryable_at_claim_and_send(tmp_path, monkeypatch):
    runner = setup_runner(tmp_path, monkeypatch)
    primary = runner.adapters[Platform.DISCORD]
    # Unknown/wrong owners, incomplete anchors (including an ownerless row),
    # and partial credentials never become primary delivery authority.
    tasks = [completion(profile=owner) for owner in ("other", "default")]
    tasks += [completion(profile=None, metadata={"scope_id": "guild"})]
    tasks += [completion(metadata=meta) for meta in (
        {"parent_chat_id": "parent"}, {"guild_id": "guild"},
        {"guild_id": "wrong", "parent_chat_id": "parent"},
    )]
    assert not collect(runner)
    assert all(unseen(task) for task in tasks)

    good = completion()
    runner._profile_adapters["yuki"] = {Platform.TELEGRAM: RecordingAdapter()}
    assert not collect(runner)
    runner._profile_adapters["yuki"] = {}
    # A tombstoned (deleted) owner profile is no longer served by the multiplexer.
    from hermes_constants import clear_named_profile_deleted, mark_named_profile_deleted
    yuki_home = tmp_path / ".hermes" / "profiles" / "yuki"
    mark_named_profile_deleted(yuki_home)
    assert not collect(runner)
    clear_named_profile_deleted(yuki_home)
    rows = collect(runner)
    assert [row["task"].id for row in rows] == [good]
    # Reassignment after the claim must rewind, never send using stale authority.
    runner.config.profile_routes = parse_profile_routes([
        dict(platform="discord", guild_id="guild", chat_id="parent", profile="other")])
    asyncio.run(deliver(runner, rows))
    assert primary.sent == primary.handled == []
    assert unseen(good)

    # Equal-specificity rules retain configuration order: an unknown parent
    # cannot skip an earlier rule, but a known conflicting parent rules it out.
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "tied-routes.db"))
    runner.config.profile_routes = parse_profile_routes([
        dict(platform="discord", guild_id="guild", chat_id="parent", profile="other"),
        dict(platform="discord", guild_id="guild", chat_id="post", profile="yuki"),
    ])
    ambiguous = completion(metadata={"scope_id": "guild"})
    exact = completion(metadata={"scope_id": "guild", "parent_chat_id": "different-parent"})
    rows = collect(runner)
    assert [row["task"].id for row in rows] == [exact]
    asyncio.run(deliver(runner, rows))
    assert len(primary.sent) == len(primary.handled) == 1
    assert unseen(ambiguous)


def test_kanban_wakes_install_the_destination_runtime_scope(tmp_path, monkeypatch):
    from agent.secret_scope import get_secret
    from gateway.run import _profile_runtime_scope
    from hermes_constants import get_hermes_home

    runner = setup_runner(tmp_path, monkeypatch)
    home = tmp_path / ".hermes"
    (home / ".env").write_text("KANBAN_TEST_SECRET=primary\n", encoding="utf-8")
    observed = []

    class ScopedAdapter(RecordingAdapter):
        async def handle_message(self, event):
            # A real yield catches scopes that mutate process-global state.
            await asyncio.sleep(0)
            observed.append((event.source.profile, get_secret("KANBAN_TEST_SECRET"), get_hermes_home()))
            await super().handle_message(event)

    for name in ("yuki", "other"):
        (home / "profiles" / name / ".env").write_text(f"KANBAN_TEST_SECRET={name}\n", encoding="utf-8")
        runner._profile_adapters[name] = {Platform.DISCORD: ScopedAdapter()}
        completion(profile=name)
    rows = collect(runner)
    assert len(rows) == 2

    async def concurrent_wakes():
        with _profile_runtime_scope(home):
            await asyncio.gather(*(_KanbanNotification(runner, row, platform_cls=Platform,
                                                       sub_fail_counts={}).deliver() for row in rows))
            assert get_secret("KANBAN_TEST_SECRET") == "primary"
    asyncio.run(concurrent_wakes())
    assert sorted(observed) == [(name, name, home / "profiles" / name) for name in ("other", "yuki")]


def test_removed_profile_never_wakes_under_the_primary_runtime(tmp_path, monkeypatch):
    import shutil

    runner = setup_runner(tmp_path, monkeypatch)
    secondary = RecordingAdapter()
    runner._profile_adapters["yuki"] = {Platform.DISCORD: secondary}
    task = completion(mode="wake")
    rows = collect(runner)
    assert len(rows) == 1
    shutil.rmtree(tmp_path / ".hermes" / "profiles" / "yuki")
    asyncio.run(deliver(runner, rows))
    assert secondary.handled == []
    assert unseen(task)


def test_anchorless_thread_subscription_warns_once_instead_of_silent_skip(tmp_path, monkeypatch, caplog):
    """A CLI-created Discord thread sub with no ``parent_chat_id`` cannot match a channel-level
    route and is skipped fail-closed — that skip must be visible ONCE at WARNING, not buried at
    DEBUG on every tick forever (#110919)."""
    import logging
    from gateway import kanban_watchers_notifier as notifier

    runner = setup_runner(tmp_path, monkeypatch)
    monkeypatch.setattr(notifier, "_ANCHORLESS_WARNED", set())
    task = completion(metadata={"chat_type": "thread"})
    with caplog.at_level(logging.WARNING, logger=notifier.logger.name):
        assert not collect(runner)
        assert not collect(runner)
    warnings = [r for r in caplog.records if "parent_chat_id" in r.getMessage() and task in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "--parent-chat-id" in warnings[0].getMessage()
    assert unseen(task)
