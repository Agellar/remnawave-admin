"""Сторож адресов: несколько пробных подписок с одного IP.

HWID клиент называет сам и подделывает заголовком, адрес выдаёт провайдер —
поэтому связка по адресу переживает то, что HWID-детект уже не видит. Но за
адресом мобильного оператора стоит CGNAT на весь район, и там несколько разных
людей с пробными — норма; отсюда отдельный порог и тесты на него.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from web.backend.core import ip_guard

IP = "91.201.236.46"


def _user(name, trial=True, conns=5, **over):
    user = {
        "uuid": f"uuid-{name}", "username": name, "telegram_id": 100,
        "email": None, "status": "ACTIVE", "is_trial": trial, "is_active": True,
        "conns": conns, "last_seen": None, "created_at": None, "expire_at": None,
    }
    user.update(over)
    return user


def _group(users, mobile=False, **over):
    group = {
        "ip": IP, "accounts": len(users), "is_mobile": mobile,
        "is_proxy": False, "is_hosting": False,
        "metadata_known": True,
        "asn_org": "PVimpelCom" if mobile else "Rostelecom", "country_code": "RU",
        "users": users,
    }
    group.update(over)
    return group


async def _run(group, *, enabled=True, notified=False, settings=None):
    values = {"violations_ip_trial_guard_enabled": enabled}
    values.update(settings or {})

    db = MagicMock()
    db.is_connected = True
    db.get_shared_ip_accounts = AsyncMock(return_value=[group] if group else [])

    cfg = MagicMock()
    cfg.get = MagicMock(side_effect=lambda key, default=None: values.get(key, default))

    notify = AsyncMock()
    with patch("shared.database.db_service", db), \
         patch("shared.config_service.config_service", cfg), \
         patch.object(ip_guard, "_recently_notified", AsyncMock(return_value=notified)), \
         patch("web.backend.core.notification_service.create_notification", notify):
        reported = await ip_guard.run_once()
    return reported, notify


class TestThresholds:
    @pytest.mark.asyncio
    async def test_three_trials_on_home_ip_are_allowed(self):
        users = [_user(name) for name in ("a", "b", "c")]
        reported, notify = await _run(_group(users))
        assert reported == 0
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fourth_trial_on_home_ip_is_reported(self):
        users = [_user(name) for name in ("a", "b", "c", "d")]
        reported, notify = await _run(_group(users))
        assert reported == 1
        notify.assert_awaited_once()
        assert notify.await_args.kwargs["source_id"] == IP

    @pytest.mark.asyncio
    async def test_mobile_ip_needs_higher_count(self):
        """CGNAT оператора: двое с пробными за одним адресом — не событие."""
        reported, notify = await _run(_group([_user("a"), _user("b")], mobile=True))
        assert reported == 0
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mobile_ip_is_disabled_by_default(self):
        users = [_user(f"u{i}") for i in range(20)]
        reported, notify = await _run(_group(users, mobile=True))
        assert reported == 0
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mobile_ip_can_use_an_explicit_high_threshold(self):
        users = [_user(f"u{i}") for i in range(11)]
        reported, _ = await _run(
            _group(users, mobile=True),
            settings={"violations_ip_trial_accounts_mobile": 10},
        )
        assert reported == 1

    @pytest.mark.asyncio
    async def test_paid_accounts_do_not_count(self):
        """За домашним адресом живёт квартира — платные подписки это норма."""
        group = _group([_user("a"), _user("b", trial=False), _user("c", trial=False)])
        reported, notify = await _run(group)
        assert reported == 0
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_geoip_is_not_treated_as_residential_abuse(self):
        users = [_user(name) for name in ("a", "b", "c", "d")]
        reported, notify = await _run(_group(users, metadata_known=False, asn_org=None))
        assert reported == 0
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", ["is_hosting", "is_proxy"])
    async def test_shared_service_exit_is_excluded(self, flag):
        users = [_user(name) for name in ("a", "b", "c", "d")]
        reported, notify = await _run(_group(users, **{flag: True}))
        assert reported == 0
        notify.assert_not_awaited()

    def test_first_residential_threshold_is_warning_not_critical(self):
        assert ip_guard._tone(_group([_user(str(i)) for i in range(4)])) == "warning"
        assert ip_guard._tone(_group([_user(str(i)) for i in range(6)])) == "critical"


class TestSilence:
    @pytest.mark.asyncio
    async def test_recently_notified_is_skipped(self):
        users = [_user(name) for name in ("a", "b", "c", "d")]
        reported, notify = await _run(_group(users), notified=True)
        assert reported == 0
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_setting_stops_guard(self):
        reported, notify = await _run(_group([_user("a"), _user("b")]), enabled=False)
        assert reported == 0
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_threshold_disables(self):
        reported, _ = await _run(
            _group([_user("a"), _user("b")]),
            settings={"violations_ip_trial_accounts": 0},
        )
        assert reported == 0


class TestCard:
    def test_card_lists_accounts_and_provider(self):
        card = ip_guard.build_card(
            _group([_user("first", conns=41), _user("second", conns=1)]),
            [_user("first", conns=41), _user("second", conns=1)],
        )
        assert card.splitlines()[0].startswith("\U0001f310 <b>")
        assert IP in card
        assert "Rostelecom" in card
        assert "Подключений: <b>41</b>" in card

    def test_mobile_card_says_so(self):
        users = [_user("a"), _user("b")]
        card = ip_guard.build_card(_group(users, mobile=True), users)
        assert "CGNAT" in card

    def test_long_list_is_collapsed(self):
        users = [_user(f"u{i}") for i in range(9)]
        card = ip_guard.build_card(_group(users), users)
        assert "<blockquote expandable>" in card
        assert "И ещё 4" in card

    def test_keyboard_carries_ip_actions(self):
        keyboard = ip_guard.build_keyboard(IP)
        data = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
        assert f"ipact:block:{IP}" in data
        assert f"ipact:users:{IP}" in data
        assert f"ipact:mute:{IP}" in data

    def test_callback_data_fits_telegram_limit(self):
        """Telegram режет callback_data на 64 байтах — с IPv6 это близко к краю."""
        keyboard = ip_guard.build_keyboard("2001:0db8:85a3:0000:0000:8a2e:0370:7334")
        for row in keyboard["inline_keyboard"]:
            for button in row:
                assert len(button["callback_data"].encode()) <= 64
