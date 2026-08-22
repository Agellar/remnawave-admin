"""Focused compatibility and false-positive guards for Smart Support 1.4.3."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

PLUGIN_SRC = Path(__file__).resolve().parents[3] / "plugins-src"
sys.path.insert(0, str(PLUGIN_SRC))

from rwa_smart_support import ai, data, outages, rules, settings, store  # noqa: E402
from rwa_smart_support import api as support_api  # noqa: E402
from rwa_smart_support.schemas import ActionExecuteIn, AIProviderIn, FeedbackIn  # noqa: E402


class FakeSettings:
    def __init__(self, values=None):
        self.values = dict(values or {})

    async def get(self, key, default=None):
        return self.values.get(key, default)

    async def set(self, key, value):
        self.values[key] = value


@pytest.mark.asyncio
async def test_ai_settings_are_qcode_sonnet_first_and_secret_safe():
    config = FakeSettings()
    resolved = await settings.patch_ai_settings(
        config,
        {"keys": {"qcode": "test-secret"}, "outage_lookup_enabled": True},
    )

    public = settings.public_ai_settings(resolved)
    assert public["provider_chain"][0] == "qcode"
    assert public["providers"][0] == {
        "provider": "qcode",
        "key_set": True,
        "model": "claude-sonnet-4-6",
    }
    assert public["outage_lookup_enabled"] is True
    assert "test-secret" not in repr(public)


@pytest.mark.asyncio
async def test_ai_key_can_be_cleared_without_legacy_resurrection():
    config = FakeSettings()
    await settings.patch_ai_settings(config, {"keys": {"qcode": "test-secret"}})
    resolved = await settings.patch_ai_settings(config, {"keys": {"qcode": ""}})

    assert settings.public_ai_settings(resolved)["providers"][0]["key_set"] is False
    assert not (await settings.get_ai_provider(config)).get("api_key")


@pytest.mark.asyncio
async def test_model_only_edit_preserves_a_legacy_qcode_key():
    config = FakeSettings({
        settings.KEY_AI_PROVIDER: {
            "provider": "qcode",
            "api_key": "legacy-test-secret",
            "model": "claude-sonnet-4-6",
        },
    })
    resolved = await settings.patch_ai_settings(
        config,
        {"models": {"qcode": "claude-sonnet-4-6"}},
    )

    assert settings.public_ai_settings(resolved)["providers"][0]["key_set"] is True
    assert (await settings.get_ai_provider(config))["api_key"] == "legacy-test-secret"


def test_common_multi_device_profile_is_not_reported_as_abuse():
    stats = {
        "total": 12,
        "ips": 12,
        "asns": 4,
        "nodes": 6,
        "short_sessions": 0,
        "country_names": ["RU", "UA"],
    }
    assert data._anomalies(stats) == []

    user = {
        "hwid_limit": 3,
        "hwid_devices": [
            {"is_blacklisted": False},
            {"is_blacklisted": False},
            {"is_blacklisted": False},
        ],
    }
    assert rules._devices_limit(user) == []


def test_legacy_provider_get_redacts_proxy_credentials_and_query_tokens():
    public = settings.redact({
        "provider": "qcode",
        "api_key": "test-secret",
        "proxy": "https://operator:proxy-secret@proxy.example:8443/path?token=hidden",
    })
    assert public["has_api_key"] is True
    assert public["proxy"] == "https://proxy.example:8443"
    assert "proxy-secret" not in repr(public)
    assert "hidden" not in repr(public)

    path_token = settings.redact({
        "provider": "qcode",
        "base_url": "https://relay.example/v1/qot_token_must_not_leak",
    })
    assert path_token["base_url"] == "https://relay.example"
    assert "qot_token_must_not_leak" not in repr(path_token)


@pytest.mark.asyncio
async def test_switching_legacy_provider_never_reuses_old_transport_or_loses_key():
    config = FakeSettings({
        settings.KEY_AI_PROVIDER: {
            "provider": "qcode",
            "api_key": "qcode-test-key",
            "model": "claude-sonnet-4-6",
            "base_url": "https://relay.invalid/custom",
            "proxy": "https://proxy.invalid/token-path",
        },
    })

    selected = await settings.patch_ai_provider(config, {
        "provider": "gemini",
        "api_key": "gemini-test-key",
        "model": "gemini-2.5-flash",
    })
    providers = {item["provider"]: item for item in await settings.get_ai_providers(config)}

    assert selected["provider"] == "gemini"
    assert not selected.get("base_url")
    assert not selected.get("proxy")
    assert providers["gemini"]["api_key"] == "gemini-test-key"
    assert providers["qcode"]["api_key"] == "qcode-test-key"
    assert providers["qcode"]["base_url"] == "https://relay.invalid/custom"
    assert providers["qcode"]["proxy"] == "https://proxy.invalid/token-path"
    assert providers["gemini"].get("base_url") != "https://relay.invalid/custom"


@pytest.mark.asyncio
async def test_switching_provider_without_new_key_fails_closed():
    config = FakeSettings({
        settings.KEY_AI_PROVIDER: {
            "provider": "qcode",
            "api_key": "qcode-only-secret",
            "base_url": "https://relay.invalid/custom",
        },
    })

    selected = await settings.patch_ai_provider(config, {"provider": "gemini"})
    providers = {item["provider"]: item for item in await settings.get_ai_providers(config)}

    # The effective provider falls back to the still-configured QCode entry;
    # the legacy selection itself is stored as Gemini without a borrowed key.
    assert selected["provider"] == "qcode"
    assert config.values[settings.KEY_AI_PROVIDER]["provider"] == "gemini"
    assert "api_key" not in config.values[settings.KEY_AI_PROVIDER]
    assert providers["gemini"].get("api_key") is None
    assert providers["qcode"]["api_key"] == "qcode-only-secret"


@pytest.mark.asyncio
async def test_environment_key_stays_runtime_only_and_provider_bound(monkeypatch):
    monkeypatch.setenv("SMART_SUPPORT_AI_PROVIDER", "qcode")
    monkeypatch.setenv("SMART_SUPPORT_AI_API_KEY", "qcode-env-only-secret")
    config = FakeSettings()

    resolved = await settings.patch_ai_settings(
        config, {"outage_lookup_enabled": False}
    )
    providers = {item["provider"]: item for item in await settings.get_ai_providers(config)}
    assert providers["qcode"]["api_key"] == "qcode-env-only-secret"
    assert providers["qcode"]["source"] == "env"
    assert "qcode-env-only-secret" not in repr(config.values)
    assert settings.public_ai_settings(resolved)["outage_lookup_enabled"] is False

    await settings.patch_ai_provider(config, {"provider": "gemini"})
    providers = {item["provider"]: item for item in await settings.get_ai_providers(config)}
    assert providers["gemini"].get("api_key") is None
    assert providers["qcode"]["api_key"] == "qcode-env-only-secret"
    assert "qcode-env-only-secret" not in repr(config.values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    ["qcode_openai", "qcode_gemini", "custom", "deepseek", "openai"],
)
async def test_legacy_only_providers_remain_effective(provider):
    config = FakeSettings({
        settings.KEY_AI_PROVIDER: {
            "provider": provider,
            "api_key": "legacy-provider-secret",
            "model": "legacy-model",
            "base_url": "https://legacy-relay.invalid/v1",
        },
    })

    selected = await settings.get_ai_provider(config)
    assert await settings.ai_enabled(config) is True
    assert selected["provider"] == provider
    assert selected["api_key"] == "legacy-provider-secret"
    assert selected["base_url"] == "https://legacy-relay.invalid/v1"


@pytest.mark.asyncio
async def test_transport_change_without_reentering_key_disables_provider():
    config = FakeSettings({
        settings.KEY_AI_PROVIDER: {
            "provider": "qcode",
            "api_key": "hidden-qcode-secret",
        },
    })

    await settings.patch_ai_provider(config, {
        "base_url": "https://attacker.invalid/collect",
    })
    providers = {item["provider"]: item for item in await settings.get_ai_providers(config)}
    assert providers["qcode"].get("api_key") is None
    assert "hidden-qcode-secret" not in repr(config.values)


def test_sonnet_prompt_and_output_guard_are_support_only():
    prompt = ai.build_system_prompt("ru").lower()
    assert "не поиск нарушителей" in prompt
    assert "несколько устройств" in prompt
    assert "никогда не предлагай блокировку" in prompt

    cleaned = ai._clean_hypotheses([
        {"title": "unsafe", "suggested_action": "disable_user"},
        {"title": "safe", "suggested_action": "switch_node"},
    ])
    assert cleaned[0]["suggested_action"] is None
    assert cleaned[1]["suggested_action"] == "switch_node"


@pytest.mark.asyncio
async def test_ioda_lookup_is_bounded_to_asn_and_obeys_toggle(monkeypatch):
    history = {
        "timeline": [
            {"asn": "AS12345", "asn_org": "Example ISP"},
            {"asn": "AS12345", "asn_org": "Example ISP"},
            {"asn": "AS54321", "asn_org": "Other ISP"},
        ],
    }
    probe = AsyncMock(return_value={
        "has_outage": True,
        "severity": "warning",
        "methods": ["bgp"],
    })
    monkeypatch.setattr(outages, "_probe", probe)
    outages.clear_cache()

    assert await outages.signal_for(history, enabled=False) is None
    probe.assert_not_awaited()
    signal = await outages.signal_for(history, enabled=True)
    probe.assert_awaited_once_with(12345)
    assert signal == {
        "asn": "AS12345",
        "org": "Example ISP",
        "severity": "warning",
        "methods": ["bgp"],
        "source": "ioda",
    }


@pytest.mark.asyncio
async def test_ai_usage_reservation_is_atomic_and_counts_attempts():
    db = AsyncMock()
    db.fetchval.return_value = 7
    assert await store.reserve_ai_usage(db, 10) == 7
    sql, period, limit = db.fetchval.await_args.args
    assert "WHERE $2 = 0 OR smart_support_ai_usage.used < $2" in sql
    assert period == store.current_period()
    assert limit == 10


@pytest.mark.parametrize(
    "address, expected",
    [
        ("100.64.10.1", False),
        ("10.0.0.1", False),
        ("127.0.0.1", False),
        ("::1", False),
        ("1.1.1.1", True),
    ],
)
def test_only_global_unicast_addresses_enter_heuristics(address, expected):
    assert data.is_globally_routable_ip(address) is expected


@pytest.mark.asyncio
async def test_soft_deleted_hwid_devices_are_filtered():
    now = datetime.now(timezone.utc)
    db = AsyncMock()
    db.fetchrow.return_value = {
        "raw_data": {},
        "traffic_limit_bytes": 0,
        "used_traffic_bytes": 0,
        "expire_at": None,
        "uuid": "00000000-0000-0000-0000-000000000001",
        "short_uuid": "short",
        "username": "user",
        "email": None,
        "telegram_id": None,
        "status": "ACTIVE",
        "created_at": now,
        "subscription_uuid": None,
        "hwid_device_limit": 2,
        "id": 1,
    }
    db.fetch.return_value = []

    await data.user_section(db, "00000000-0000-0000-0000-000000000001")

    assert "d.removed_at IS NULL" in db.fetch.await_args.args[0]


@pytest.mark.asyncio
async def test_correlation_refresh_is_an_upsert_not_an_append():
    db = AsyncMock()
    now = datetime.now(timezone.utc)
    await store.replace_correlations(
        db,
        [{
            "kind": "node",
            "key": "node-1",
            "label": "Node 1",
            "affected_users": 5,
            "total_users": 10,
            "member_uuids": ["user-1"],
            "window_start": now,
            "window_end": now,
        }],
        180,
    )

    insert_sql = db.execute.await_args_list[-1].args[0]
    assert "ON CONFLICT (kind, key) DO UPDATE" in insert_sql
    assert "last_active_at = NOW()" in insert_sql
    assert any(
        "WHERE kind = 'asn'" in call.args[0]
        for call in db.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_policy_violations_on_one_asn_never_become_provider_outage():
    db = AsyncMock()
    db.fetch.side_effect = [[], []]

    clusters = await data.compute_clusters(db, settings.DEFAULT_THRESHOLDS)
    queried_sql = "\n".join(call.args[0] for call in db.fetch.await_args_list)

    assert clusters == []
    assert "FROM violations" not in queried_sql
    assert rules._clusters([{
        "kind": "asn",
        "key": "AS12345",
        "label": "Large ISP",
        "affected_users": 5,
        "is_active": True,
    }]) == []


@pytest.mark.asyncio
async def test_clients_facade_uses_cloud_and_returns_graceful_503(monkeypatch):
    class FakeCloudError(Exception):
        def __init__(self, code, detail=None):
            super().__init__(code)
            self.code = code
            self.detail = detail

    permissions = []

    def auth_deps():
        def require_permission(resource, action):
            permissions.append((resource, action))

            async def allowed():
                return None

            return allowed

        return object, require_permission

    plugin_api = types.ModuleType("web.backend.core.plugin_api")
    plugin_api.CloudError = FakeCloudError
    plugin_api.auth_deps = auth_deps
    monkeypatch.setitem(sys.modules, "web.backend.core.plugin_api", plugin_api)

    cloud = SimpleNamespace(
        client_apps=AsyncMock(return_value={
            "apps": [{"id": "happ", "ambiguous": False, "issues": []}],
            "catalog_version": 4,
        }),
        client_submissions=AsyncMock(),
        submit_client_change=AsyncMock(),
        vote_client_submission=AsyncMock(),
    )
    ctx = SimpleNamespace(
        db=AsyncMock(),
        logger=MagicMock(),
        cloud=cloud,
        settings=FakeSettings(),
        telemetry=SimpleNamespace(count=lambda *_: None),
        events=SimpleNamespace(emit=lambda *_: None),
    )
    router = support_api.build_router(ctx)
    endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/clients" and "GET" in route.methods
    )

    result = await endpoint(None)
    assert result.catalog_version == 4
    assert result.apps[0].id == "happ"
    assert ("smart_support", "view") in permissions
    assert ("smart_support", "edit") in permissions

    cloud.client_apps.side_effect = FakeCloudError("server_unreachable", "internal")
    with pytest.raises(HTTPException) as exc_info:
        await endpoint(None)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {"code": "server_unreachable"}


@pytest.mark.asyncio
async def test_provider_cooldown_is_reported_without_upstream_details(monkeypatch):
    plugin_api = types.ModuleType("web.backend.core.plugin_api")
    plugin_api.CloudError = type("FakeCloudError", (Exception,), {})

    def auth_deps():
        def require_permission(_resource, _action):
            async def allowed():
                return None
            return allowed
        return object, require_permission

    plugin_api.auth_deps = auth_deps
    monkeypatch.setitem(sys.modules, "web.backend.core.plugin_api", plugin_api)

    user_uuid = "00000000-0000-0000-0000-000000000001"
    monkeypatch.setattr(support_api.settings_mod, "get_thresholds", AsyncMock(return_value={
        "correlation_max_age_minutes": 60,
        "correlation_history_minutes": 180,
    }))
    monkeypatch.setattr(support_api.settings_mod, "ai_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(support_api.settings_mod, "get_client_versions", AsyncMock(return_value={}))
    monkeypatch.setattr(support_api.settings_mod, "get_ai_providers", AsyncMock(return_value=[{
        "provider": "qcode",
        "api_key": "test-secret",
        "model": "claude-sonnet-4-6",
        "monthly_limit": 0,
    }]))
    monkeypatch.setattr(support_api.data, "user_section", AsyncMock(return_value={"uuid": user_uuid}))
    monkeypatch.setattr(support_api.data, "history_section", AsyncMock(return_value={}))
    monkeypatch.setattr(support_api.data, "client_section", AsyncMock(return_value={}))
    monkeypatch.setattr(support_api.data, "nodes_section", AsyncMock(return_value=[]))
    monkeypatch.setattr(support_api.data, "violations_section", AsyncMock(return_value=[]))
    monkeypatch.setattr(support_api.store, "correlations_for_user", AsyncMock(return_value=[]))
    monkeypatch.setattr(support_api.store, "log_action", AsyncMock(return_value=1))
    monkeypatch.setattr(support_api.rules, "evaluate", MagicMock(return_value=[]))
    monkeypatch.setattr(support_api, "active_for_nodes", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        support_api.ai,
        "analyze",
        AsyncMock(side_effect=support_api.ai.AIError("http_429: upstream-private-detail")),
    )

    ctx = SimpleNamespace(
        db=AsyncMock(),
        logger=MagicMock(),
        cloud=SimpleNamespace(),
        settings=FakeSettings(),
        telemetry=SimpleNamespace(count=lambda *_: None),
        events=SimpleNamespace(emit=lambda *_: None),
    )
    router = support_api.build_router(ctx)
    report_endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/report/{user_uuid}" and "GET" in route.methods
    )
    status_endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/ai-status" and "GET" in route.methods
    )

    report = await report_endpoint(user_uuid, SimpleNamespace(username="operator"))
    assert report.ai_analysis is None
    status = await status_endpoint(None)
    qcode = status.providers[0]
    assert qcode.available is False
    assert qcode.cooldown_seconds_remaining > 0
    assert qcode.last_error == "http_429"
    assert "test-secret" not in repr(status)
    assert "upstream-private-detail" not in repr(status)


@pytest.mark.asyncio
async def test_scoped_admin_cannot_observe_or_mutate_hidden_users(monkeypatch):
    import shared.rbac as shared_rbac

    plugin_api = types.ModuleType("web.backend.core.plugin_api")
    plugin_api.CloudError = type("FakeCloudError", (Exception,), {})

    def auth_deps():
        def require_permission(_resource, _action):
            async def allowed():
                return None
            return allowed
        return object, require_permission

    plugin_api.auth_deps = auth_deps
    monkeypatch.setitem(sys.modules, "web.backend.core.plugin_api", plugin_api)

    visible_uuid = "00000000-0000-0000-0000-000000000001"
    hidden_uuid = "00000000-0000-0000-0000-000000000002"
    resolver = AsyncMock(return_value={visible_uuid})
    monkeypatch.setattr(shared_rbac, "get_visible_user_uuids", resolver)
    monkeypatch.setattr(support_api.data, "search_users", AsyncMock(return_value=("username", [])))
    monkeypatch.setattr(support_api.actions_mod, "execute", AsyncMock())
    monkeypatch.setattr(support_api.store, "save_feedback", AsyncMock())
    monkeypatch.setattr(support_api.store, "sessions_for_user", AsyncMock())
    monkeypatch.setattr(support_api.store, "sessions_recent", AsyncMock(return_value=([], 0)))

    ctx = SimpleNamespace(
        db=AsyncMock(),
        logger=MagicMock(),
        cloud=SimpleNamespace(),
        settings=FakeSettings(),
        telemetry=SimpleNamespace(count=lambda *_: None),
        events=SimpleNamespace(emit=lambda *_: None),
    )
    router = support_api.build_router(ctx)

    def endpoint(path, method):
        return next(
            route.endpoint for route in router.routes
            if route.path == path and method in route.methods
        )

    admin = SimpleNamespace(account_id=42, role="support", username="scoped")
    await endpoint("/search", "GET")("alice", 20, admin)
    assert support_api.data.search_users.await_args.kwargs["visible_user_uuids"] == {visible_uuid}

    hidden_calls = [
        lambda: endpoint("/report/{user_uuid}", "GET")(hidden_uuid, admin),
        lambda: endpoint("/feedback", "POST")(
            FeedbackIn(user_uuid=hidden_uuid, rule_id="rule", verdict="wrong"), admin
        ),
        lambda: endpoint("/actions/{action_id}/execute", "POST")(
            "disable_user", ActionExecuteIn(user_uuid=hidden_uuid), admin
        ),
        lambda: endpoint("/sessions/user/{user_uuid}", "GET")(
            hidden_uuid, 20, 0, admin
        ),
    ]
    for invoke in hidden_calls:
        with pytest.raises(HTTPException) as exc_info:
            await invoke()
        assert exc_info.value.status_code == 404

    support_api.actions_mod.execute.assert_not_awaited()
    support_api.store.save_feedback.assert_not_awaited()
    support_api.store.sessions_for_user.assert_not_awaited()

    await endpoint("/sessions/recent", "GET")(50, 0, None, None, admin)
    assert support_api.store.sessions_recent.await_args.kwargs["visible_user_uuids"] == {visible_uuid}

    with pytest.raises(HTTPException) as exc_info:
        await endpoint("/ai-provider", "PUT")(
            AIProviderIn(base_url="https://attacker.invalid/collect"), admin
        )
    assert exc_info.value.status_code == 403

    # Resolver faults are authorization faults: return an empty result set and
    # never fall back to unrestricted visibility.
    resolver.side_effect = RuntimeError("scope backend unavailable")
    await endpoint("/search", "GET")("alice", 20, admin)
    assert support_api.data.search_users.await_args.kwargs["visible_user_uuids"] == set()
    with pytest.raises(HTTPException) as exc_info:
        await endpoint("/report/{user_uuid}", "GET")(visible_uuid, admin)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_annulled_violations_do_not_enter_smart_support_context():
    db = AsyncMock()
    db.fetch.return_value = []
    await data.violations_section(db, "00000000-0000-0000-0000-000000000001")
    assert "action_taken IS DISTINCT FROM 'annulled'" in db.fetch.await_args.args[0]
