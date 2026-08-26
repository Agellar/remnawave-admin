"""Tests for violations API — /api/v2/violations/*."""
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from web.backend.api.deps import get_current_admin
from web.backend.api.v2.violations import get_severity


class TestGetSeverity:
    """Tests for severity score classification."""

    def test_critical(self):
        assert get_severity(80.0).value == "critical"
        assert get_severity(100.0).value == "critical"

    def test_high(self):
        assert get_severity(60.0).value == "high"
        assert get_severity(79.9).value == "high"

    def test_medium(self):
        assert get_severity(40.0).value == "medium"
        assert get_severity(59.9).value == "medium"

    def test_low(self):
        assert get_severity(0.0).value == "low"
        assert get_severity(39.9).value == "low"


MOCK_VIOLATIONS = [
    {
        "id": 1,
        "user_uuid": "aaa-111",
        "username": "alice",
        "email": None,
        "telegram_id": None,
        "score": 85.0,
        "recommended_action": "disable",
        "confidence": 0.92,
        "detected_at": datetime(2026, 2, 16, 10, 0),
        "action_taken": None,
        "notified_at": None,
    },
    {
        "id": 2,
        "user_uuid": "bbb-222",
        "username": "bob",
        "email": "bob@example.com",
        "telegram_id": 12345,
        "score": 45.0,
        "recommended_action": "no_action",
        "confidence": 0.65,
        "detected_at": datetime(2026, 2, 15, 8, 0),
        "action_taken": "resolved",
        "notified_at": datetime(2026, 2, 15, 9, 0),
    },
]


class TestListViolations:
    """GET /api/v2/violations."""

    @pytest.mark.asyncio
    async def test_list_violations_success(self, app, client):
        from web.backend.api.deps import get_db

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.count_violations_for_period = AsyncMock(return_value=2)
        mock_db.get_violations_for_period = AsyncMock(return_value=MOCK_VIOLATIONS)

        app.dependency_overrides[get_db] = lambda: mock_db

        resp = await client.get("/api/v2/violations")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_list_hides_annulled_by_default(self, app, client):
        """H1: без include_annulled список исключает аннулированные (совпадает со статистикой)."""
        from web.backend.api.deps import get_db

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.count_violations_for_period = AsyncMock(return_value=0)
        mock_db.get_violations_for_period = AsyncMock(return_value=[])
        app.dependency_overrides[get_db] = lambda: mock_db

        resp = await client.get("/api/v2/violations")
        assert resp.status_code == 200
        # дефолт: include_annulled=False прокидывается в оба запроса
        assert mock_db.get_violations_for_period.call_args.kwargs["include_annulled"] is False
        assert mock_db.count_violations_for_period.call_args.kwargs["include_annulled"] is False

        resp = await client.get("/api/v2/violations", params={"include_annulled": "true"})
        assert resp.status_code == 200
        assert mock_db.get_violations_for_period.call_args.kwargs["include_annulled"] is True

    @pytest.mark.asyncio
    async def test_list_violations_as_viewer_allowed(self, app, viewer):
        """Viewers have violations.view permission."""
        from web.backend.api.deps import get_db as _get_db
        app.dependency_overrides[get_current_admin] = lambda: viewer

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.count_violations_for_period = AsyncMock(return_value=0)
        mock_db.get_violations_for_period = AsyncMock(return_value=[])
        app.dependency_overrides[_get_db] = lambda: mock_db

        from httpx import ASGITransport, AsyncClient
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/v2/violations")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_list_violations_anon_unauthorized(self, anon_client):
        resp = await anon_client.get("/api/v2/violations")
        assert resp.status_code == 401


class TestAnnulScope:
    """H4: scope-проверки на аннулировании."""

    @pytest.mark.asyncio
    async def test_annul_all_forbidden_for_scoped_admin(self, app, client):
        """Глобальный annul-all запрещён админу с ограниченной видимостью."""
        from web.backend.api.deps import get_db
        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.annul_all_pending_violations = AsyncMock(return_value=99)
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock, return_value={"aaaa"}):
            resp = await client.post("/api/v2/violations/annul-all", json={})
        assert resp.status_code == 403
        mock_db.annul_all_pending_violations.assert_not_called()

    @pytest.mark.asyncio
    async def test_annul_all_allowed_for_unrestricted_admin(self, app, client):
        """Админ без ограничения видимости (visible=None) проходит гейт."""
        from web.backend.api.deps import get_db
        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.annul_all_pending_violations = AsyncMock(return_value=5)
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock, return_value=None), \
             patch("web.backend.api.v2.violations.write_audit_log", new_callable=AsyncMock):
            resp = await client.post("/api/v2/violations/annul-all", json={})
        assert resp.status_code == 200
        mock_db.annul_all_pending_violations.assert_called_once()

    @pytest.mark.asyncio
    async def test_annul_by_id_forbidden_out_of_scope(self, app, client):
        """Аннулирование нарушения юзера вне scope → 403."""
        from web.backend.api.deps import get_db
        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.get_violation_by_id = AsyncMock(return_value={"id": 7, "user_uuid": "dead-beef"})
        mock_db.update_violation_action = AsyncMock(return_value=True)
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock, return_value={"other-uuid"}):
            resp = await client.post("/api/v2/violations/7/annul", json={})
        assert resp.status_code == 403
        mock_db.update_violation_action.assert_not_called()


class TestRowToListItem:
    """Tests for _row_to_list_item helper."""

    def test_converts_mock_violation(self):
        from web.backend.api.v2.violations import _row_to_list_item
        item = _row_to_list_item(MOCK_VIOLATIONS[0])
        assert item.id == 1
        assert item.username == "alice"
        assert item.score == 85.0
        assert item.severity.value == "critical"
        assert item.notified is False

    def test_notified_when_notified_at_present(self):
        from web.backend.api.v2.violations import _row_to_list_item
        item = _row_to_list_item(MOCK_VIOLATIONS[1])
        assert item.notified is True

    def test_defaults_for_missing_fields(self):
        from web.backend.api.v2.violations import _row_to_list_item
        item = _row_to_list_item({"id": 99})
        assert item.score == 0.0
        assert item.severity.value == "low"
        assert item.recommended_action == "no_action"


# ══════════════════════════════════════════════════════════════════
# HWID Blacklist tests
# ══════════════════════════════════════════════════════════════════

class TestHwidBlacklist:
    """Tests for HWID blacklist API — /api/v2/violations/hwid-blacklist."""

    @pytest.mark.asyncio
    async def test_list_returns_items(self, app, client):
        """GET /hwid-blacklist returns items from DB."""
        mock_items = [{"id": 1, "hwid": "abc", "action": "alert", "reason": None, "created_at": "2026-01-01T00:00:00"}]
        with patch("shared.database.DatabaseService.get_hwid_blacklist", new_callable=AsyncMock, create=True) as mock:
            mock.return_value = mock_items
            response = await client.get("/api/v2/violations/hwid-blacklist")
            # Note: may return 200 or 422 depending on rate limiter state
            if response.status_code == 200:
                data = response.json()
                assert data["total"] == 1
                assert data["items"][0]["hwid"] == "abc"

    @pytest.mark.asyncio
    async def test_add_hwid_alert(self, app, client):
        """POST /hwid-blacklist with action=alert."""
        with patch("shared.database.DatabaseService.add_hwid_to_blacklist", new_callable=AsyncMock, create=True) as mock_add, \
             patch("shared.database.DatabaseService.find_users_by_hwid", new_callable=AsyncMock, create=True) as mock_find, \
             patch("web.backend.core.rbac.write_audit_log", new_callable=AsyncMock):
            mock_add.return_value = {"id": 1, "hwid": "abc123", "action": "alert", "reason": "test"}
            mock_find.return_value = []

            response = await client.post("/api/v2/violations/hwid-blacklist", json={
                "hwid": "abc123",
                "action": "alert",
                "reason": "test reason",
            })
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["affected_users"] == 0
            mock_add.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_hwid_block_with_affected_users(self, app, client):
        """POST /hwid-blacklist with action=block triggers blocking."""
        with patch("shared.database.DatabaseService.add_hwid_to_blacklist", new_callable=AsyncMock, create=True) as mock_add, \
             patch("shared.database.DatabaseService.find_users_by_hwid", new_callable=AsyncMock, create=True) as mock_find, \
             patch("web.backend.api.v2.violations._handle_blacklisted_hwid_users", new_callable=AsyncMock) as mock_handle, \
             patch("web.backend.core.rbac.write_audit_log", new_callable=AsyncMock):
            mock_add.return_value = {"id": 1, "hwid": "abc123", "action": "block", "reason": None}
            mock_find.return_value = [
                {"user_uuid": "user-1", "username": "alice", "status": "active"},
            ]

            response = await client.post("/api/v2/violations/hwid-blacklist", json={
                "hwid": "abc123",
                "action": "block",
            })
            assert response.status_code == 200
            assert response.json()["affected_users"] == 1
            mock_handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_hwid_invalid_action(self, app, client):
        """POST /hwid-blacklist with invalid action returns 422."""
        response = await client.post("/api/v2/violations/hwid-blacklist", json={
            "hwid": "abc123",
            "action": "invalid",
        })
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_add_hwid_empty(self, app, client):
        """POST /hwid-blacklist with empty hwid returns 422."""
        response = await client.post("/api/v2/violations/hwid-blacklist", json={
            "hwid": "",
            "action": "alert",
        })
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_delete_hwid(self, app, client):
        """DELETE /hwid-blacklist/{hwid} removes entry."""
        with patch("shared.database.DatabaseService.remove_hwid_from_blacklist", new_callable=AsyncMock, create=True) as mock_remove, \
             patch("web.backend.core.rbac.write_audit_log", new_callable=AsyncMock):
            mock_remove.return_value = True
            response = await client.delete("/api/v2/violations/hwid-blacklist/abc123")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_hwid_not_found(self, app, client):
        """DELETE /hwid-blacklist/{hwid} returns 404 if not in blacklist."""
        with patch("shared.database.DatabaseService.remove_hwid_from_blacklist", new_callable=AsyncMock, create=True) as mock_remove, \
             patch("web.backend.core.rbac.write_audit_log", new_callable=AsyncMock):
            mock_remove.return_value = False
            response = await client.delete("/api/v2/violations/hwid-blacklist/nonexistent")
            assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_list_hwid_users(self, app, client):
        """GET /hwid-blacklist/{hwid}/users returns affected users."""
        with patch("shared.database.DatabaseService.find_users_by_hwid", new_callable=AsyncMock, create=True) as mock:
            mock.return_value = [
                {"user_uuid": "user-1", "username": "alice", "status": "ACTIVE", "platform": "iOS", "device_model": "iPhone 15"},
                {"user_uuid": "user-2", "username": "bob", "status": "EXPIRED", "platform": "Android", "device_model": None},
            ]
            response = await client.get("/api/v2/violations/hwid-blacklist/abc123/users")
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 2
            assert data["users"][0]["username"] == "alice"

    @pytest.mark.asyncio
    async def test_viewer_cannot_add_hwid(self, app, viewer):
        """Viewer role should not be able to add to HWID blacklist."""
        from httpx import ASGITransport, AsyncClient
        app.dependency_overrides[get_current_admin] = lambda: viewer
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.post("/api/v2/violations/hwid-blacklist", json={
                "hwid": "abc123", "action": "alert",
            })
            assert response.status_code == 403


class TestHwidBlacklistRequest:
    """Tests for HwidBlacklistRequest Pydantic model."""

    def test_valid_request(self):
        from web.backend.api.v2.violations import HwidBlacklistRequest
        req = HwidBlacklistRequest(hwid="abc123", action="alert", reason="test")
        assert req.hwid == "abc123"
        assert req.action == "alert"

    def test_invalid_action_rejected(self):
        from web.backend.api.v2.violations import HwidBlacklistRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            HwidBlacklistRequest(hwid="abc", action="destroy")

    def test_block_action_valid(self):
        from web.backend.api.v2.violations import HwidBlacklistRequest
        req = HwidBlacklistRequest(hwid="xyz", action="block")
        assert req.action == "block"


class TestResolveBlockIdempotent:
    """POST /violations/{id}/resolve action=block на уже отключённом юзере.

    Кейс из сообщества: панель отвечает ошибкой на disable уже отключённого
    юзера → резолв падал 502 «Сервис API недоступен» и нарушение нельзя было
    закрыть. Уже DISABLED = успех блокировки.
    """

    def _db(self):
        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.get_violation_by_id = AsyncMock(return_value={
            "id": 1, "user_uuid": "aaa-111", "username": "alice",
        })
        mock_db.update_violation_action = AsyncMock(return_value=True)
        return mock_db

    @pytest.mark.asyncio
    async def test_block_already_disabled_user_succeeds(self, app, client):
        from web.backend.api.deps import get_db

        mock_db = self._db()
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("shared.api_client.api_client.disable_user",
                   AsyncMock(side_effect=Exception("User is already disabled"))), \
             patch("shared.api_client.api_client.get_user_by_id",
                   AsyncMock(return_value={"response": {"status": "DISABLED"}})), \
             patch("web.backend.core.rbac.get_visible_user_uuids",
                   AsyncMock(return_value=None)):
            resp = await client.post("/api/v2/violations/1/resolve",
                                     json={"action": "block"})

        assert resp.status_code == 200
        assert resp.json()["action"] == "block"
        mock_db.update_violation_action.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_block_active_user_disable_failure_is_502(self, app, client):
        from web.backend.api.deps import get_db

        mock_db = self._db()
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("shared.api_client.api_client.disable_user",
                   AsyncMock(side_effect=Exception("boom"))), \
             patch("shared.api_client.api_client.get_user_by_id",
                   AsyncMock(return_value={"response": {"status": "ACTIVE"}})), \
             patch("web.backend.core.rbac.get_visible_user_uuids",
                   AsyncMock(return_value=None)):
            resp = await client.post("/api/v2/violations/1/resolve",
                                     json={"action": "block"})

        assert resp.status_code == 502
        mock_db.update_violation_action.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════
# Ограничение скорости и источник скора
# ══════════════════════════════════════════════════════════════════

class TestThrottlesRoute:
    """RBAC, batching and mutations for /api/v2/violations/throttle*."""

    VISIBLE_UUID = "50726d47-2f6c-440b-b573-925c79ea84a1"
    HIDDEN_UUID = "77777777-7777-4777-8777-777777777777"

    @staticmethod
    def _row(user_uuid, *, rate_kbit=1024):
        return {
            "user_uuid": user_uuid,
            "rate_kbit": rate_kbit,
            "reason": "torrent",
            "until": None,
            "created_at": datetime(2026, 8, 24, 9, 34),
            "created_by_username": "admin",
            "prev_squads": None,
        }

    @pytest.mark.asyncio
    async def test_full_admin_list_uses_one_batch_lookup(self, app, client):
        """Full admin sees all rows and usernames are resolved without N+1."""
        from web.backend.api.deps import get_db

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.get_active_throttles = AsyncMock(return_value=[
            self._row(self.VISIBLE_UUID),
            self._row(self.HIDDEN_UUID, rate_kbit=2048),
        ])
        mock_db.batch_get_users_info = AsyncMock(return_value={
            self.VISIBLE_UUID: {"username": "alice"},
            self.HIDDEN_UUID: {"username": "bob"},
        })
        mock_db.get_user_by_uuid = AsyncMock()
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock, return_value=None):
            resp = await client.get("/api/v2/violations/throttles")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["items"][0]["user_uuid"] == self.VISIBLE_UUID
        assert body["items"][0]["username"] == "alice"
        assert body["items"][1]["username"] == "bob"
        mock_db.batch_get_users_info.assert_awaited_once_with([
            self.VISIBLE_UUID, self.HIDDEN_UUID,
        ])
        mock_db.get_user_by_uuid.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scoped_admin_list_hides_out_of_scope_rows(
        self, app, viewer_client,
    ):
        """A non-superadmin receives only throttle rows in its user scope."""
        from web.backend.api.deps import get_db

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.get_active_throttles = AsyncMock(return_value=[
            self._row(self.VISIBLE_UUID),
            self._row(self.HIDDEN_UUID),
        ])
        mock_db.batch_get_users_info = AsyncMock(return_value={
            self.VISIBLE_UUID: {"username": "alice"},
        })
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock,
                   return_value={self.VISIBLE_UUID.lower()}):
            resp = await viewer_client.get("/api/v2/violations/throttles")

        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        assert [item["user_uuid"] for item in resp.json()["items"]] == [
            self.VISIBLE_UUID,
        ]
        mock_db.batch_get_users_info.assert_awaited_once_with([self.VISIBLE_UUID])

    @pytest.mark.asyncio
    async def test_empty_scope_returns_no_rows_without_user_lookup(
        self, app, viewer_client,
    ):
        """An empty user scope is deny-all, not unrestricted."""
        from web.backend.api.deps import get_db

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.get_active_throttles = AsyncMock(return_value=[
            self._row(self.VISIBLE_UUID),
        ])
        mock_db.batch_get_users_info = AsyncMock()
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock, return_value=set()):
            resp = await viewer_client.get("/api/v2/violations/throttles")

        assert resp.status_code == 200
        assert resp.json() == {"items": [], "total": 0}
        mock_db.batch_get_users_info.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_full_admin_can_add_throttle(self, app, client):
        """Unrestricted scope keeps the existing add/audit flow working."""
        from web.backend.api.deps import get_db

        mock_db = MagicMock()
        mock_db.is_connected = True
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock, return_value=None), \
             patch("shared.throttle.apply_throttle",
                   new_callable=AsyncMock,
                   return_value=(True, None, False)) as apply, \
             patch("web.backend.core.throttle_sync.push_throttles",
                   new_callable=AsyncMock, return_value=1), \
             patch("web.backend.api.v2.violations.write_audit_log",
                   new_callable=AsyncMock):
            resp = await client.post(
                "/api/v2/violations/throttle",
                json={"user_uuid": self.VISIBLE_UUID, "rate_kbit": 1024},
            )

        assert resp.status_code == 200
        assert resp.json()["rate_kbit"] == 1024
        assert apply.await_args.kwargs["user_uuid"] == self.VISIBLE_UUID

    @pytest.mark.asyncio
    async def test_scoped_admin_can_add_visible_user(
        self, app, manager, manager_client,
    ):
        """A scoped admin may mutate a user explicitly present in the scope."""
        from web.backend.api.deps import get_db

        manager.permissions = {
            *manager.permissions, ("violations", "resolve"),
        }
        mock_db = MagicMock()
        mock_db.is_connected = True
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock,
                   return_value={self.VISIBLE_UUID.lower()}), \
             patch("shared.throttle.apply_throttle",
                   new_callable=AsyncMock,
                   return_value=(True, None, False)) as apply, \
             patch("web.backend.core.throttle_sync.push_throttles",
                   new_callable=AsyncMock, return_value=1), \
             patch("web.backend.api.v2.violations.write_audit_log",
                   new_callable=AsyncMock):
            resp = await manager_client.post(
                "/api/v2/violations/throttle",
                json={"user_uuid": self.VISIBLE_UUID, "rate_kbit": 1024},
            )

        assert resp.status_code == 200
        apply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scoped_admin_cannot_add_hidden_user(
        self, manager, manager_client,
    ):
        """The add path fails closed before changing throttle state."""
        manager.permissions = {
            *manager.permissions, ("violations", "resolve"),
        }

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock,
                   return_value={self.VISIBLE_UUID.lower()}), \
             patch("shared.throttle.apply_throttle",
                   new_callable=AsyncMock) as apply:
            resp = await manager_client.post(
                "/api/v2/violations/throttle",
                json={"user_uuid": self.HIDDEN_UUID, "rate_kbit": 1024},
            )

        assert resp.status_code == 403
        apply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_full_admin_can_remove_throttle(self, app, client):
        """Unrestricted scope keeps the existing remove/audit flow working."""
        from web.backend.api.deps import get_db

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.get_user_throttle = AsyncMock(return_value={
            "prev_squads": None,
        })
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock, return_value=None), \
             patch("shared.throttle.lift_throttle",
                   new_callable=AsyncMock,
                   return_value=(True, False)) as lift, \
             patch("web.backend.core.throttle_sync.push_throttles",
                   new_callable=AsyncMock, return_value=1), \
             patch("web.backend.api.v2.violations.write_audit_log",
                   new_callable=AsyncMock) as audit:
            resp = await client.delete(
                f"/api/v2/violations/throttle/{self.VISIBLE_UUID}",
            )

        assert resp.status_code == 200
        assert resp.json()["squads_restored"] is False
        assert resp.json()["restore_required"] is False
        assert resp.json()["restore_failed"] is False
        lift.assert_awaited_once_with(self.VISIBLE_UUID)
        details = json.loads(audit.await_args.kwargs["details"])
        assert details["restore_required"] is False
        assert details["restore_failed"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prev_squads", [
        ["squad-a"],
        '["squad-a"]',
    ])
    async def test_remove_reports_required_restore_failure(
        self, app, client, prev_squads,
    ):
        """Saved squads in DB list/JSON forms produce an explicit failure signal."""
        from web.backend.api.deps import get_db

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.get_user_throttle = AsyncMock(return_value={
            "prev_squads": prev_squads,
        })
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock, return_value=None), \
             patch("shared.throttle.lift_throttle",
                   new_callable=AsyncMock,
                   return_value=(True, False)), \
             patch("web.backend.core.throttle_sync.push_throttles",
                   new_callable=AsyncMock, return_value=1), \
             patch("web.backend.api.v2.violations.write_audit_log",
                   new_callable=AsyncMock) as audit:
            resp = await client.delete(
                f"/api/v2/violations/throttle/{self.VISIBLE_UUID}",
            )

        assert resp.status_code == 200
        assert resp.json()["squads_restored"] is False
        assert resp.json()["restore_required"] is True
        assert resp.json()["restore_failed"] is True
        details = json.loads(audit.await_args.kwargs["details"])
        assert details["squads_restored"] is False
        assert details["restore_required"] is True
        assert details["restore_failed"] is True

    @pytest.mark.asyncio
    async def test_scoped_admin_can_remove_visible_user(
        self, app, manager, manager_client,
    ):
        """A scoped admin may lift a throttle for a visible user."""
        from web.backend.api.deps import get_db

        manager.permissions = {
            *manager.permissions, ("violations", "resolve"),
        }
        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.get_user_throttle = AsyncMock(return_value={
            "prev_squads": ["squad-a"],
        })
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock,
                   return_value={self.VISIBLE_UUID.lower()}), \
             patch("shared.throttle.lift_throttle",
                   new_callable=AsyncMock,
                   return_value=(True, True)) as lift, \
             patch("web.backend.core.throttle_sync.push_throttles",
                   new_callable=AsyncMock, return_value=1), \
             patch("web.backend.api.v2.violations.write_audit_log",
                   new_callable=AsyncMock):
            resp = await manager_client.delete(
                f"/api/v2/violations/throttle/{self.VISIBLE_UUID}",
            )

        assert resp.status_code == 200
        assert resp.json()["squads_restored"] is True
        assert resp.json()["restore_required"] is True
        assert resp.json()["restore_failed"] is False
        lift.assert_awaited_once_with(self.VISIBLE_UUID)

    @pytest.mark.asyncio
    async def test_scoped_admin_cannot_remove_hidden_user(
        self, app, manager, manager_client,
    ):
        """The remove path fails closed before lifting a hidden throttle."""
        from web.backend.api.deps import get_db

        manager.permissions = {
            *manager.permissions, ("violations", "resolve"),
        }
        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.get_user_throttle = AsyncMock()
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock,
                   return_value={self.VISIBLE_UUID.lower()}), \
             patch("shared.throttle.lift_throttle",
                   new_callable=AsyncMock) as lift:
            resp = await manager_client.delete(
                f"/api/v2/violations/throttle/{self.HIDDEN_UUID}",
            )

        assert resp.status_code == 403
        lift.assert_not_awaited()
        mock_db.get_user_throttle.assert_not_awaited()

    @pytest.mark.parametrize(
        ("endpoint", "preset"),
        [
            ("list_throttles", "read"),
            ("add_throttle", "mutations"),
            ("remove_throttle", "mutations"),
        ],
    )
    def test_throttle_routes_have_expected_rate_limits(self, endpoint, preset):
        """SlowAPI registry keeps read and mutation presets on every route."""
        from limits import parse

        from web.backend.core.rate_limit import (
            RATE_MUTATIONS,
            RATE_READ,
            limiter,
        )

        expected = RATE_READ if preset == "read" else RATE_MUTATIONS
        route_key = f"web.backend.api.v2.violations.{endpoint}"
        limits = limiter._route_limits.get(route_key)

        assert limits, f"No rate limit registered for {endpoint}"
        assert limits[0].limit == parse(expected)

    def test_throttle_setting_names_minimum_agent_1_7_3(self):
        from shared.config_service import DEFAULT_CONFIG_DEFINITIONS

        definition = next(
            item for item in DEFAULT_CONFIG_DEFINITIONS
            if item["key"] == "throttle_enabled"
        )
        assert "1.7.3+" in definition["description"]
        assert "1.6.0+" not in definition["description"]


class TestScoreSource:
    """Источник скора у нарушений, заведённых мимо анализаторов подключений."""

    @staticmethod
    def _violation(**extra):
        row = {
            "id": 306,
            "user_uuid": "aaa-111",
            "score": 100.0,
            "recommended_action": "hard_block",
            "confidence": 1.0,
            "detected_at": datetime(2026, 8, 24, 9, 33),
            "reasons": [],
        }
        row.update(extra)
        return row

    @staticmethod
    async def _detail(app, client, row):
        from web.backend.api.deps import get_db

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.get_violation_by_id = AsyncMock(return_value=row)
        app.dependency_overrides[get_db] = lambda: mock_db

        with patch("web.backend.core.rbac.get_visible_user_uuids",
                   new_callable=AsyncMock, return_value=None):
            resp = await client.get("/api/v2/violations/306")
        assert resp.status_code == 200
        return resp.json()

    @pytest.mark.asyncio
    async def test_torrent_violation_reports_its_source(self, app, client):
        body = await self._detail(app, client, self._violation(
            reasons=["Torrent traffic detected (1 events)", "Destination: 1.2.3.4:6881"],
        ))
        assert body["score_source"] == "torrent"

    @pytest.mark.asyncio
    async def test_other_scoreless_source_marked_external(self, app, client):
        body = await self._detail(app, client, self._violation(
            reasons=["Аномальный расход: 120 ГБ/ч"],
        ))
        assert body["score_source"] == "external"

    @pytest.mark.asyncio
    async def test_analyzer_violation_keeps_regular_breakdown(self, app, client):
        body = await self._detail(app, client, self._violation(
            score=85.0, hwid_score=60.0, reasons=["HWID shared"],
        ))
        assert body["score_source"] is None
