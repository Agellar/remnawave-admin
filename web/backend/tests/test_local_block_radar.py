"""Regression tests for the self-hosted, local-only Block Radar."""
from __future__ import annotations

import pytest

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from rwa_local_block_radar import ai, engine, qcode, settings, store
from rwa_local_block_radar.api import _alert, _local_id, _probe_schedule
from rwa_local_block_radar.plugin import manifest


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("VLESS_REALITY", "reality"),
        ("reality-xhttp", "reality+xhttp"),
        ("vless-splithttp", "xhttp"),
        ("vless-ws", "ws"),
        ("grpc-main", "grpc"),
        ("", "mixed"),
    ],
)
def test_transport_classification(tag, expected):
    assert engine.classify_transport(tag) == expected


def test_thresholds_are_clamped_to_safe_ranges():
    result = settings._validated(
        {
            "dip_frac": 0.01,
            "dip_min_users": 0,
            "dip_confirm_ticks": 1,
            "dip_history_days": 999,
            "online_window_minutes": 0,
        }
    )
    assert result["dip_frac"] == 0.30
    assert result["dip_min_users"] == 3
    assert result["dip_confirm_ticks"] == 2
    assert result["dip_history_days"] == 30
    assert result["online_window_minutes"] == 1
    assert result["notify_enabled"] is False
    assert "node_probe_enabled" not in result
    assert "node_probe_vantages" not in result


def test_node_dip_requires_both_share_and_absolute_drop():
    cfg = settings._validated({"dip_frac": 0.65, "dip_min_users": 8})
    base = {"online": 100.0, "share": 0.25}
    assert engine._is_dip({"online": 20, "share": 0.05}, base, cfg) is True
    assert engine._is_dip({"online": 90, "share": 0.05}, base, cfg) is False
    assert engine._is_dip({"online": 20, "share": 0.20}, base, cfg) is False


def test_local_provider_ids_are_stable_and_not_public_asns():
    assert _local_id("UPCLOUD") == _local_id("UPCLOUD")
    assert _local_id("UPCLOUD") < 0
    assert _local_id("TIMEWEB") != _local_id("UPCLOUD")


def test_manifest_uses_builtin_block_radar_ui_without_license():
    item = manifest()
    assert item.id == "block_radar"
    assert item.billing == "free"
    assert item.version == "0.7.3"
    assert "edit" in item.rbac_resources["block_radar"]
    assert item.navigation[0].path == "/plugins/block-radar"
    assert item.navigation[0].permission == ("block_radar", "view")
    assert {task.name for task in item.build(type("Ctx", (), {})()).scheduled_tasks} == {
        "local-radar", "globalping"
    }


def test_probe_schedule_is_safe_for_api_output():
    item = _probe_schedule({
        "probe_running": True,
        "last_probe_at": "2026-08-14T08:00:00+00:00",
        "next_probe_at_epoch": 1_786_344_060,
        "probe_interval_seconds": 60,
    })
    assert item["probe_running"] is True
    assert item["next_probe_at"].endswith("+00:00")
    assert item["probe_interval_seconds"] == 60


def test_schema_prevents_duplicate_open_incidents():
    assert "local_block_radar_one_open_alert_idx" in store.DDL
    assert "WHERE resolved_at IS NULL" in store.DDL
    assert "feedback TEXT" in store.DDL
    assert "feedback_at TIMESTAMPTZ" in store.DDL


def test_ai_schema_and_prompt_are_infrastructure_only():
    assert "local_block_radar_ai_analyses" in store.DDL
    assert "local_block_radar_ai_usage" in store.DDL
    assert "user_uuid" not in store.DDL
    assert "telegram_id" not in store.DDL
    assert "likely_block" in ai.SYSTEM_PROMPT
    assert "provider_outage" in ai.SYSTEM_PROMPT
    assert "node_failure" in ai.SYSTEM_PROMPT
    assert "traffic_shift" in ai.SYSTEM_PROMPT
    assert "insufficient_data" in ai.SYSTEM_PROMPT
    assert "не доказывает" in ai.SYSTEM_PROMPT


def test_ai_is_pinned_to_sonnet_and_block_confidence_is_calibrated():
    cfg = settings._validated({"ai_model": "gpt-5", "ai_monthly_limit": 9999})
    assert cfg["ai_model"] == "claude-sonnet-4-6"
    assert cfg["ai_monthly_limit"] == 1000
    result = ai._validate(
        {
            "classification": "likely_block",
            "confidence": 0.98,
            "summary": "Избирательная просадка.",
            "evidence": ["Нода жива"],
            "recommendations": ["Сравнить внешние пробы"],
            "support_note": "Наблюдаем сетевую деградацию.",
        }
    )
    assert result["confidence"] == 0.70


def test_alert_api_decodes_jsonb_ai_arrays():
    now = datetime.now(timezone.utc)
    row = {
        "id": 5,
        "provider_name": "provider-a",
        "node_name": "node-a",
        "node_alive": True,
        "transport": "reality",
        "since": now,
        "resolved_at": None,
        "online": 4,
        "baseline_online": 20,
        "ai_id": 7,
        "ai_classification": "traffic_shift",
        "ai_confidence": 0.6,
        "ai_summary": "summary",
        "ai_evidence": '["one"]',
        "ai_recommendations": '["two"]',
        "ai_support_note": "note",
        "ai_provider": "qcode",
        "ai_model": "claude-sonnet-4-6",
        "ai_created_at": now,
        "ai_updated_at": now,
        "feedback": "false_positive",
        "feedback_at": now,
    }
    item = _alert(row)
    assert item["ai_analysis"]["evidence"] == ["one"]
    assert item["ai_analysis"]["recommendations"] == ["two"]
    assert item["feedback"] == "false_positive"
    assert item["feedback_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_feedback_update_is_parameterized():
    db = AsyncMock()
    now = datetime.now(timezone.utc)
    db.fetchrow.return_value = {
        "id": 11,
        "feedback": "confirmed",
        "feedback_at": now,
    }
    result = await store.set_feedback(db, 11, "confirmed")
    query, alert_id, verdict = db.fetchrow.await_args.args
    assert "WHERE id=$1" in query
    assert "feedback=$2" in query
    assert alert_id == 11
    assert verdict == "confirmed"
    assert result["feedback"] == "confirmed"


@pytest.mark.asyncio
async def test_ai_call_uses_sonnet_tool_and_sanitized_context(monkeypatch):
    class FakeSettings:
        async def get(self, key, default=None):
            return default

    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "record_radar_analysis",
                        "input": {
                            "classification": "traffic_shift",
                            "confidence": 0.62,
                            "summary": "Остальная сеть выросла, нода доступна.",
                            "evidence": ["node_alive=true"],
                            "recommendations": ["Сравнить транспорт на соседней ноде"],
                            "support_note": "Наблюдаем перераспределение нагрузки.",
                        },
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, *, json, headers):
            captured.update(url=url, body=json, headers=headers)
            return Response()

    monkeypatch.setattr(ai.httpx, "AsyncClient", lambda **_: Client())
    monkeypatch.setattr(
        ai,
        "get_ai_provider",
        AsyncMock(return_value={"provider": "qcode", "api_key": "secret"}),
    )
    monkeypatch.setattr(store, "analysis_for_alert", AsyncMock(return_value=None))
    monkeypatch.setattr(
        store,
        "alert_by_id",
        AsyncMock(return_value={
            "id": 9,
            "node_uuid": "00000000-0000-0000-0000-000000000009",
            "node_name": "node-a",
            "provider_name": "provider-a",
            "transport": "reality",
            "since": datetime.now(timezone.utc),
            "resolved_at": None,
            "online": 3,
            "baseline_online": 20,
            "share": 0.02,
            "baseline_share": 0.15,
            "node_alive": True,
        }),
    )
    context = {
        "alert": {"node_name": "node-a", "online": 3},
        "recent_samples_newest_first": [],
        "network_latest": [],
    }
    monkeypatch.setattr(store, "analysis_context", AsyncMock(return_value=context))
    monkeypatch.setattr(store, "reserve_ai_call", AsyncMock(return_value=1))

    async def saved(_db, **kwargs):
        result = kwargs["result"]
        now = datetime.now(timezone.utc)
        return {
            "id": 1,
            "alert_id": kwargs["alert_id"],
            **result,
            "provider": kwargs["provider"],
            "model": kwargs["model"],
            "created_at": now,
            "updated_at": now,
        }

    monkeypatch.setattr(store, "save_analysis", saved)
    ctx = type("Ctx", (), {"settings": FakeSettings(), "db": object()})()

    result = await ai.analyze_alert(ctx, 9)

    assert result["classification"] == "traffic_shift"
    assert captured["body"]["model"] == "claude-sonnet-4-6"
    assert captured["body"]["tool_choice"]["name"] == "record_radar_analysis"
    serialized = captured["body"]["messages"][0]["content"]
    assert "node-a" in serialized
    assert "user_uuid" not in serialized
    assert "telegram_id" not in serialized
    assert "Authorization" in captured["headers"]


@pytest.mark.asyncio
async def test_qcode_usage_is_read_only_and_strictly_allowlisted(monkeypatch):
    qcode._reset_cache()
    captured = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url, *, headers):
            captured.append((url, headers))
            if url.endswith("/me"):
                return Response({"ok": True, "data": {
                    "active_api_keys": 1,
                    "total_api_keys": 1,
                    "formatted_today_cost": "$0.16",
                    "has_any_errors": False,
                    "last_updated": "2026/08/09 17:47:56",
                    "secret": "must-not-leak",
                }})
            return Response({"ok": True, "data": {"keys": [{
                "name": "trial",
                "is_active": True,
                "expires_at": "2099-09-04",
                "current_requests": 23,
                "current_tokens": 40796,
                "formatted_current_cost": "$0.16",
                "api_key": "must-not-leak",
            }]}})

    monkeypatch.setenv("QCODE_OPENAPI_TOKEN", "qot_test_secret")
    monkeypatch.setattr(qcode.httpx, "AsyncClient", lambda **_: Client())
    result = await qcode.usage_status()

    assert result["ok"] is True
    assert result["account"]["active_api_keys"] == 1
    assert result["keys"][0]["current_requests"] == 23
    assert result["keys"][0]["expiry_warning"] == "ok"
    assert "secret" not in result["account"]
    assert "api_key" not in result["keys"][0]
    assert [url.rsplit("/", 1)[-1] for url, _ in captured] == ["me", "keys"]
    assert all(
        headers["Authorization"] == "Bearer qot_test_secret"
        for _, headers in captured
    )
    cached = await qcode.usage_status()
    assert cached == result
    assert len(captured) == 2
    qcode._reset_cache()
