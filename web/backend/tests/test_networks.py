"""Опознание сети абонента: мобильный оператор и «источник» подключения.

Повод для этих проверок — жалоба из чата: защита от шаринга ловила
человека, у которого CGNAT оператора раздал каждому соединению свой
адрес. По логам это был один клиент, открывший пачку соединений к одному
хосту за полмиллисекунды, а по адресам — «двадцать четыре разных».
"""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from shared.analyzers import networks


# ── мобильная сеть ────────────────────────────────────────────────

@pytest.mark.parametrize("org", [
    "PJSC MegaFon", "MTS PJSC", "Mobile TeleSystems JLLC", "PVimpelCom",
    "T2 Mobile LLC", "MEGAFON-AS", "Kyivstar", "Kcell",
])
def test_carrier_recognised_even_when_metadata_says_residential(org):
    """Главная причина ложняков: GeoIP зовёт мобильные сети «residential».

    В базе панели у МТС мобильными помечены 3 адреса из 39 393 — флаг
    проставляется один раз при первой встрече и больше не пересматривается.
    Имя организации приходит нормальным, по нему и опознаём.
    """
    assert networks.is_mobile_network(org, "residential") is True


@pytest.mark.parametrize("org", ["IDDQD-AS", "Selectel Ltd", "Hetzner Online GmbH", "Yandex LLC"])
def test_ordinary_networks_are_not_mobile(org):
    assert networks.is_mobile_network(org, "residential") is False


def test_connection_type_still_counts_when_it_is_honest():
    assert networks.is_mobile_network(None, "mobile") is True
    assert networks.is_mobile_network(None, "mobile_isp") is True


def test_unknown_network_is_not_mobile():
    assert networks.is_mobile_network(None, None) is False


# ── источник вместо адреса ────────────────────────────────────────

def _pool(prefix: str, start: int, stop: int, asn: int, org: str, ctype="residential"):
    return {
        f"{prefix}.{i}": {"asn": asn, "asn_org": org, "connection_type": ctype}
        for i in range(start, stop)
    }


def test_operator_pool_collapses_into_one_source():
    """Тот самый случай из жалобы: 24 адреса, за ними два человека."""
    meta = _pool("178.177.22", 2, 14, 31133, "PJSC MegaFon")
    meta.update(_pool("81.9.21", 179, 191, 16345, "PVimpelCom"))

    assert len(meta) == 24
    assert networks.count_sources(meta, meta) == 2


def test_same_subnet_different_operators_stay_apart():
    # Один и тот же /24 у разных ASN — разные сети, склеивать нельзя.
    meta = {
        "10.0.0.1": {"asn": 1, "asn_org": "A", "connection_type": "fixed"},
        "10.0.0.2": {"asn": 2, "asn_org": "B", "connection_type": "fixed"},
    }
    assert networks.count_sources(meta, meta) == 2


def test_hosting_is_never_collapsed():
    """У хостера соседние адреса — разные машины, и шаринг живёт как раз там."""
    meta = _pool("5.9.10", 1, 5, 24940, "Hetzner Online GmbH", "hosting")
    assert networks.count_sources(meta, meta) == 4


def test_vpn_and_datacenter_are_not_collapsed():
    for ctype in ("vpn", "datacenter"):
        meta = _pool("203.0.113", 1, 4, 64500, "Some Provider", ctype)
        assert networks.count_sources(meta, meta) == 3, ctype


def test_ipv6_collapses_by_subscriber_prefix():
    # Абоненту выдают /64 целиком — это его собственная сеть.
    meta = {
        "2a02:6b8:c01:1::1": {"asn": 13238, "asn_org": "Yandex", "connection_type": "fixed"},
        "2a02:6b8:c01:1::ff": {"asn": 13238, "asn_org": "Yandex", "connection_type": "fixed"},
        "2a02:6b8:c01:2::1": {"asn": 13238, "asn_org": "Yandex", "connection_type": "fixed"},
    }
    assert networks.count_sources(meta, meta) == 2


def test_without_metadata_every_address_stays_its_own_source():
    # Гадать по одному виду адреса, чей это пул, нельзя — считаем как есть.
    ips = ["1.1.1.1", "1.1.1.2", "1.1.1.3"]
    assert networks.count_sources(ips) == 3


def test_garbage_address_survives():
    mapping = networks.source_map(["не адрес", "1.1.1.1"], {})
    assert mapping["не адрес"] == "не адрес"


def test_has_mobile_network_scans_all_addresses():
    meta = {
        "1.1.1.1": {"asn": 13335, "asn_org": "Cloudflare", "connection_type": "hosting"},
        "2.2.2.2": {"asn": 25159, "asn_org": "PJSC MegaFon", "connection_type": "residential"},
    }
    assert networks.has_mobile_network(meta) is True
    assert networks.has_mobile_network({"1.1.1.1": meta["1.1.1.1"]}) is False


# ── классификация ASN не должна терять оператора ──────────────────

@pytest.mark.asyncio
async def test_asn_database_does_not_override_carrier_name():
    """Локальная база ASN не должна разжаловать оператора в «домашнюю сеть».

    Она наполняется из RIPE, где вместо названия часто приходит хендл вида
    ORG-OM1-RIPE — классификатор оператора по нему не узнаёт и ставит
    дефолтный residential. Именно из-за этого МегаФон и МТС оказывались
    домашними сетями, а CGNAT-буфер детектора не включался.
    """
    from unittest.mock import AsyncMock

    from shared.geoip import GeoIPService

    db = AsyncMock()
    db.is_connected = True
    db.get_asn_record = AsyncMock(return_value={"provider_type": "residential",
                                                "region": "Москва", "city": "Москва"})
    service = GeoIPService(db_service=db)

    ctype, is_mobile, is_dc, is_vpn, region, city = await service._classify_asn(
        25159, "PJSC MegaFon", False, False, "RU",
    )
    assert (ctype, is_mobile) == ("mobile", True)
    # регион и город из базы при этом не теряются
    assert (region, city) == ("Москва", "Москва")


@pytest.mark.asyncio
async def test_asn_database_still_wins_for_ordinary_networks():
    from unittest.mock import AsyncMock

    from shared.geoip import GeoIPService

    db = AsyncMock()
    db.is_connected = True
    db.get_asn_record = AsyncMock(return_value={"provider_type": "hosting"})
    service = GeoIPService(db_service=db)

    ctype, is_mobile, is_dc, _, _, _ = await service._classify_asn(
        24940, "Hetzner Online GmbH", False, False, "RU",
    )
    assert (ctype, is_mobile, is_dc) == ("hosting", False, True)


# ── trial-IP guard query ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_shared_ip_query_accepts_trial_internal_squads_without_tags():
    """A deployment may mark trials only through an internal squad.

    The SQL prefilter must use the same tag-or-squad contract as
    ``_is_trial_user``; otherwise the guard silently sees zero trial users.
    """
    from shared.db.network import NetworkMixin

    connection = AsyncMock()
    connection.fetch = AsyncMock(return_value=[])

    class _Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Database(NetworkMixin):
        is_connected = True

        def acquire(self):
            return _Acquire()

    with patch("shared.db.network._load_trial_settings", return_value=([], ["trial-squad-uuid"])):
        assert await _Database().get_shared_ip_accounts() == []

    sql, *args = connection.fetch.await_args.args
    assert "activeInternalSquads" in sql
    assert args[-1] == ["trial-squad-uuid"]


def test_unlimited_hwid_limit_is_not_mapped_to_one_device():
    from shared.db.network import UNLIMITED_DEVICE_ALLOWANCE, _device_allowance_from_raw

    count = _device_allowance_from_raw({"hwidDeviceLimit": 0, "devicesCount": 4})
    assert count >= 4
    assert count == UNLIMITED_DEVICE_ALLOWANCE


@pytest.mark.asyncio
async def test_single_and_batch_device_lookups_preserve_unlimited_semantics():
    from shared.db import DatabaseService
    from shared.db.network import UNLIMITED_DEVICE_ALLOWANCE

    class _Acquire:
        async def __aenter__(self):
            connection = AsyncMock()
            connection.fetchrow = AsyncMock(return_value={
                "raw_data": {"hwidDeviceLimit": 0, "devicesCount": 4},
            })
            connection.fetch = AsyncMock(return_value=[{
                "uuid": "00000000-0000-0000-0000-000000000001",
                "raw_data": {"hwidDeviceLimit": 0, "devicesCount": 4},
            }])
            return connection

        async def __aexit__(self, exc_type, exc, tb):
            return False

    database = DatabaseService()
    database._pool = SimpleNamespace(_closed=False)
    database.acquire = lambda: _Acquire()
    user_uuid = "00000000-0000-0000-0000-000000000001"

    assert await database.get_user_devices_count(user_uuid) == UNLIMITED_DEVICE_ALLOWANCE
    assert (await database.batch_get_user_devices_counts([user_uuid]))[user_uuid] == UNLIMITED_DEVICE_ALLOWANCE
