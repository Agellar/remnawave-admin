"""The event and peer thresholds must use the same, safe evidence window."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import shared.db.connections as connections_db
from shared.db.connections import (
    ConnectionsMixin,
    TORRENT_SHARED_MAX_EVENTS,
    TORRENT_SHARED_TIMEOUT_SECONDS,
    TORRENT_WINDOW_MAX_DESTINATIONS,
    torrent_destination_ip,
)
from web.backend.api.v2 import collector
from web.backend.core import torrent_p2p_whitelist


USER_UUID = "11111111-2222-3333-4444-555555555555"
NODE_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
USER_EMAIL = "test-user@example.invalid"


@pytest.mark.parametrize("destination,expected", [
    ("203.0.113.9:6881", "203.0.113.9"),
    ("203.0.113.9:51413", "203.0.113.9"),
    ("[2001:0db8:0:0:0:0:0:1]:6881", "2001:db8::1"),
    ("2001:db8::1:51413", "2001:db8::1"),
    ("[::ffff:203.0.113.9]:6881", "203.0.113.9"),
    ("::ffff:cb00:7109:51413", "203.0.113.9"),
    ("example.invalid:6881", None),
    ("203.0.113.9", None),
    ("2001:db8::1", None),
    ("[2001:db8::1]", None),
    ("203.0.113.9:0", None),
    ("203.0.113.9:65536", None),
    ("203.0.113.9:" + "9" * 5000, None),
    ("[fe80::1%eth0]:6881", None),
])
def test_peer_identity_is_a_canonical_ip_not_an_endpoint(destination, expected):
    assert torrent_destination_ip(destination) == expected


def _database(rows=None, error=None):
    connection = MagicMock()
    connection.fetch = AsyncMock(return_value=rows or [], side_effect=error)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=connection)
    context.__aexit__ = AsyncMock(return_value=False)
    database = ConnectionsMixin()
    database.is_connected = True
    database.acquire = MagicMock(return_value=context)
    return database, connection


@pytest.mark.asyncio
async def test_window_is_grouped_bounded_and_excludes_future_events():
    database, connection = _database([
        {"destination": "203.0.113.9:6881", "event_count": 6},
    ])
    assert await database.get_recent_torrent_destination_counts(USER_UUID) == {
        "203.0.113.9:6881": 6,
    }
    query, user_uuid, minutes, limit = connection.fetch.call_args.args
    assert "GROUP BY destination LIMIT $3" in query
    assert "detected_at <= NOW()" in query
    assert (user_uuid, minutes, limit) == (USER_UUID, 30, TORRENT_WINDOW_MAX_DESTINATIONS + 1)


@pytest.mark.asyncio
async def test_oversized_or_failed_window_is_not_partial_evidence():
    database, _ = _database([
        {"destination": f"endpoint-{number}", "event_count": 1}
        for number in range(TORRENT_WINDOW_MAX_DESTINATIONS + 1)
    ])
    assert await database.get_recent_torrent_destination_counts(USER_UUID) is None
    assert await database.count_recent_torrent_peers(USER_UUID) == 0

    database, _ = _database(error=RuntimeError("database unavailable"))
    assert await database.get_recent_torrent_destination_counts(USER_UUID) is None
    assert await database.count_recent_torrent_peers(USER_UUID) == 0

    database.is_connected = False
    assert await database.get_recent_torrent_destination_counts(USER_UUID) is None


@pytest.mark.asyncio
async def test_peer_count_deduplicates_ports_ipv6_spellings_and_mapped_ipv4():
    database, _ = _database([
        {"destination": destination, "event_count": 3}
        for destination in (
            "203.0.113.9:6881", "203.0.113.9:51413", "[::ffff:203.0.113.9]:60000",
            "[2001:db8::1]:6881", "2001:0db8:0:0:0:0:0:1:51413",
            "example.invalid:6881",
        )
    ])
    assert await database.count_recent_torrent_peers(USER_UUID) == 2


@pytest.mark.asyncio
async def test_shared_filter_failure_excludes_all_candidates_when_used_as_evidence():
    database, _ = _database(error=RuntimeError("database unavailable"))
    destinations = ["203.0.113.9:6881", "203.0.113.10:6881"]
    assert await database.shared_torrent_destinations(destinations, fail_closed=True) == set(destinations)
    database.is_connected = False
    assert await database.shared_torrent_destinations(destinations, fail_closed=True) == set(destinations)


@pytest.mark.asyncio
async def test_shared_filter_normalizes_historical_aliases_and_different_ports():
    rows = [
        {"destination": destination, "user_uuid": owner, "window_rows": 5}
        for destination, owner in (
            ("[2001:db8::1]:6881", "user-a"),
            ("2001:0db8:0:0:0:0:0:1:51413", "user-b"),
            ("203.0.113.9:6881", "user-a"),
            ("[::ffff:cb00:7109]:51413", "user-b"),
            ("203.0.113.10:6881", "user-a"),
        )
    ]
    database, connection = _database(rows)
    targets = ["2001:db8::1:60000", "[::ffff:203.0.113.9]:60000", "203.0.113.10:6881"]
    assert await database.shared_torrent_destinations(targets, fail_closed=True) == set(targets[:2])
    query, hours, row_limit = connection.fetch.call_args.args
    assert (hours, row_limit) == (24, TORRENT_SHARED_MAX_EVENTS + 1)
    assert "WITH recent AS MATERIALIZED" in query
    assert "ORDER BY detected_at DESC" in query
    assert query.index("LIMIT $2") < query.index("GROUP BY")
    assert "WHERE size.total >= $2" in query  # overflow-only marker
    assert connection.fetch.call_args.kwargs["timeout"] == TORRENT_SHARED_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_one_user_with_multiple_aliases_is_not_a_shared_ip():
    database, _ = _database([
        {"destination": destination, "user_uuid": USER_UUID, "window_rows": 3}
        for destination in (
            "203.0.113.9:6881", "[::ffff:203.0.113.9]:51413", "::ffff:cb00:7109:60000",
        )
    ])
    assert await database.get_recent_shared_torrent_ips() == set()


@pytest.mark.asyncio
async def test_shared_evidence_overflow_is_unknown_not_no_shared_ips():
    database, _ = _database([{
        "destination": None, "user_uuid": None,
        "window_rows": TORRENT_SHARED_MAX_EVENTS + 1,
    }])
    assert await database.get_recent_shared_torrent_ips() is None
    targets = ["203.0.113.9:6881"]
    assert await database.shared_torrent_destinations(targets, fail_closed=True) == set(targets)


@pytest.mark.asyncio
async def test_shared_evidence_timeout_is_unknown_and_sanitized(monkeypatch, caplog):
    database, connection = _database()

    async def unavailable(*_args, **_kwargs):
        await asyncio.Event().wait()

    connection.fetch.side_effect = unavailable
    monkeypatch.setattr(connections_db, "TORRENT_SHARED_TIMEOUT_SECONDS", 0.01)
    assert await database.get_recent_shared_torrent_ips() is None
    assert "deferred" in caplog.text
    assert "TimeoutError" in caplog.text

    connection.fetch.side_effect = RuntimeError("sensitive address 203.0.113.9")
    assert await database.get_recent_shared_torrent_ips() is None
    assert "203.0.113.9" not in caplog.text


@pytest.mark.parametrize("destination,infra", [
    ("[2001:0db8:0:0:0:0:0:1]:443", "2001:db8::1"),
    ("2001:db8::1:8443", "2001:0db8:0:0:0:0:0:1"),
    ("[::ffff:203.0.113.9]:443", "203.0.113.9"),
])
def test_own_service_filter_handles_equivalent_ipv6_endpoints(destination, infra):
    assert collector._is_own_service(destination, frozenset({infra}))


@pytest.fixture
def evidence(monkeypatch):
    state = SimpleNamespace(
        counts={}, organizations={}, shared=set(), infrastructure=frozenset(),
        settings={"torrent_min_events": 5, "torrent_min_peers": 3, "torrent_auto_action": "notify"},
    )
    database = MagicMock()
    database.get_recent_torrent_destination_counts = AsyncMock(side_effect=lambda *_a, **_k: state.counts)
    database.get_recent_shared_torrent_ips = AsyncMock(side_effect=lambda *_a, **_k: state.shared)
    database.is_user_violation_whitelisted = AsyncMock(return_value=(False, None))
    database.get_recent_torrent_violation = AsyncMock(return_value=None)
    database.get_user_by_uuid = AsyncMock(return_value={"username": "test-user"})
    database.save_violation = AsyncMock(return_value=(42, True))
    monkeypatch.setattr(collector, "db_service", database)
    monkeypatch.setattr(collector, "_own_infrastructure_ips", AsyncMock(side_effect=lambda: state.infrastructure))
    monkeypatch.setattr(collector.config_service, "get", lambda key, default=None: state.settings.get(key, default))

    async def lookup(ips):
        return {
            ip: SimpleNamespace(asn_org=state.organizations.get(ip, "Ordinary Network"))
            for ip in ips
        }

    state.geoip = SimpleNamespace(lookup_batch=AsyncMock(side_effect=lookup))
    monkeypatch.setattr("shared.geoip.get_geoip_service", lambda: state.geoip)
    state.notify = AsyncMock()
    monkeypatch.setattr("web.backend.core.violation_notifier.send_torrent_notification", state.notify)
    state.fire = MagicMock()
    monkeypatch.setattr(collector, "fire_event", state.fire)
    state.automation = AsyncMock()
    monkeypatch.setattr("web.backend.core.automation_engine.engine.handle_event", state.automation)
    state.broadcast = AsyncMock()
    monkeypatch.setattr("web.backend.api.v2.websocket.broadcast_violation", state.broadcast)
    state.disable = AsyncMock()
    monkeypatch.setattr("shared.api_client.api_client.disable_user", state.disable)
    state.database = database
    return state


def _event(destination):
    return collector.TorrentEventReport(
        user_email=USER_EMAIL, ip_address="192.0.2.11", node_uuid=NODE_UUID,
        destination=destination, detected_at=datetime.now(timezone.utc), detected_by="ndpi",
    )


async def _process(destinations):
    await collector._process_torrent_violations(
        [_event(destination) for destination in destinations], {USER_EMAIL: USER_UUID},
    )


def _assert_no_accusation(state):
    state.database.save_violation.assert_not_awaited()
    state.notify.assert_not_awaited()
    state.automation.assert_not_awaited()
    state.broadcast.assert_not_awaited()
    state.fire.assert_not_called()
    state.disable.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowed_p2p_cannot_amplify_one_unrelated_event(evidence):
    evidence.settings["torrent_auto_action"] = "block_user"
    evidence.counts = {
        "203.0.113.1:6881": 20, "203.0.113.2:6881": 20,
        "203.0.113.3:6881": 20, "198.51.100.9:51413": 1,
    }
    evidence.organizations = {f"203.0.113.{number}": "Gaijin Network Ltd" for number in range(1, 4)}
    await _process(list(evidence.counts))
    _assert_no_accusation(evidence)


@pytest.mark.asyncio
async def test_safe_and_shared_history_cannot_amplify_one_unrelated_event(evidence):
    evidence.counts = {
        "203.0.113.1:443": 50, "203.0.113.2:6881": 20,
        "203.0.113.3:6881": 20, "198.51.100.9:51413": 1,
    }
    evidence.infrastructure = frozenset({"203.0.113.1"})
    evidence.shared = {"203.0.113.2", "203.0.113.3"}
    await _process(["198.51.100.9:51413"])
    _assert_no_accusation(evidence)
    evidence.database.get_recent_shared_torrent_ips.assert_awaited_once_with()
    assert evidence.geoip.lookup_batch.call_args.args[0] == ["198.51.100.9"]


@pytest.mark.asyncio
@pytest.mark.parametrize("destinations", [
    ["203.0.113.9:6881", "203.0.113.9:51413", "[::ffff:203.0.113.9]:60000"],
    ["[2001:db8::1]:6881", "2001:db8::1:51413", "[2001:0db8:0:0:0:0:0:1]:60000"],
])
async def test_one_ip_with_multiple_ports_is_not_a_swarm(evidence, destinations):
    evidence.counts = {destination: 3 for destination in destinations}
    await _process(destinations)
    _assert_no_accusation(evidence)


@pytest.mark.asyncio
async def test_real_eligible_swarm_still_passes_with_history_split_across_batches(evidence):
    evidence.counts = {
        "203.0.113.1:6881": 2, "203.0.113.2:6881": 2,
        "[2001:db8::3]:51413": 1, "198.51.100.1:51413": 50,
    }
    evidence.organizations = {"198.51.100.1": "Kaspersky Lab Switzerland GmbH"}
    await _process(["[2001:db8::3]:51413", "198.51.100.1:51413"])
    evidence.database.save_violation.assert_awaited_once()
    evidence.notify.assert_awaited_once()
    assert evidence.notify.call_args.kwargs["destinations"] == ["[2001:db8::3]:51413"]
    assert len(evidence.notify.call_args.kwargs["torrent_events"]) == 1
    assert evidence.automation.call_args.args[1]["destinations"] == ["[2001:db8::3]:51413"]
    assert evidence.database.save_violation.call_args.kwargs["reasons"][:2] == [
        "Torrent traffic detected (1 events)",
        "Evidence window: 5 eligible events, 3 distinct peer IPs in 30 min",
    ]
    evidence.disable.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_users_with_three_aliased_peers_cannot_trigger_and_query_shared_once(evidence):
    other_uuid = "11111111-2222-3333-4444-555555555556"
    other_email = "second-user@example.invalid"
    user_destinations = ["[2001:db8::1]:6881", "203.0.113.9:6881", "2001:db8::2:51413"]
    other_destinations = [
        "2001:0db8:0:0:0:0:0:1:51413", "[::ffff:203.0.113.9]:60000", "[2001:0db8::2]:6881",
    ]
    windows = {
        USER_UUID: dict.fromkeys(user_destinations, 2),
        other_uuid: dict.fromkeys(other_destinations, 2),
    }
    evidence.database.get_recent_torrent_destination_counts.side_effect = (
        lambda user_uuid, **_kwargs: windows[user_uuid]
    )
    snapshot, connection = _database([
        {"destination": destination, "user_uuid": user_uuid, "window_rows": 12}
        for user_uuid, destinations in windows.items()
        for destination in destinations
    ])
    evidence.database.get_recent_shared_torrent_ips.side_effect = snapshot.get_recent_shared_torrent_ips
    evidence.settings["torrent_auto_action"] = "block_user"
    events = [_event(destination) for destination in user_destinations]
    events.extend(
        _event(destination).model_copy(update={"user_email": other_email})
        for destination in other_destinations
    )
    await collector._process_torrent_violations(events, {USER_EMAIL: USER_UUID, other_email: other_uuid})
    _assert_no_accusation(evidence)
    assert evidence.database.get_recent_torrent_destination_counts.await_count == 2
    evidence.database.get_recent_shared_torrent_ips.assert_awaited_once_with()
    connection.fetch.assert_awaited_once()
    calls = [call[0] for call in evidence.database.mock_calls]
    assert calls[:3] == [
        "get_recent_torrent_destination_counts", "get_recent_torrent_destination_counts",
        "get_recent_shared_torrent_ips",
    ]


@pytest.mark.asyncio
async def test_incomplete_shared_snapshot_defers_all_batch_decisions(evidence):
    evidence.counts = {f"203.0.113.{number}:6881": 10 for number in range(1, 4)}
    evidence.shared = None
    evidence.settings["torrent_auto_action"] = "block_user"
    await _process(list(evidence.counts))
    _assert_no_accusation(evidence)
    evidence.geoip.lookup_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowed_only_batch_does_not_retrigger_older_eligible_history(evidence):
    evidence.counts = {
        "203.0.113.1:6881": 10, "203.0.113.2:6881": 10,
        "203.0.113.3:6881": 10, "198.51.100.1:51413": 50,
    }
    evidence.organizations = {"198.51.100.1": "Valve Corp."}
    await _process(["198.51.100.1:51413"])
    _assert_no_accusation(evidence)


@pytest.mark.asyncio
async def test_missing_asn_evidence_cannot_accuse(evidence):
    evidence.counts = {f"203.0.113.{number}:6881": 10 for number in range(1, 4)}
    evidence.organizations = {f"203.0.113.{number}": None for number in range(1, 4)}
    await _process(list(evidence.counts))
    _assert_no_accusation(evidence)


@pytest.mark.asyncio
async def test_lookup_timeout_does_not_accuse(evidence, monkeypatch):
    evidence.counts = {f"203.0.113.{number}:6881": 10 for number in range(1, 4)}

    async def unavailable(_ips):
        await asyncio.Event().wait()

    evidence.geoip.lookup_batch.side_effect = unavailable
    monkeypatch.setattr(torrent_p2p_whitelist, "_LOOKUP_TIMEOUT_SECONDS", 0.01)
    await _process(list(evidence.counts))
    _assert_no_accusation(evidence)


@pytest.mark.asyncio
async def test_incomplete_window_cannot_accuse_even_with_thresholds_disabled(evidence):
    evidence.counts = None
    evidence.settings.update(torrent_min_events=1, torrent_min_peers=1)
    await _process(["203.0.113.1:6881"])
    _assert_no_accusation(evidence)


def test_badly_typed_allowlist_uses_defaults(evidence):
    evidence.settings["torrent_asn_whitelist"] = ["Gaijin"]
    assert torrent_p2p_whitelist.is_whitelisted_org("Gaijin Network Ltd")
