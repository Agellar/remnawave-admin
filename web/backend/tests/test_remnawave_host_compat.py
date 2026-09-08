"""Contract tests for Remnawave 3.2 and 3.4 host squad filters."""

import pytest
from unittest.mock import AsyncMock

from shared.api_client import RemnawaveApiClient
from shared.remnawave_compat import (
    adapt_host_internal_squads_payload,
    parse_panel_version,
    read_host_internal_squads,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3.2.0", (3, 2, 0)), ("v3.4.3", (3, 4, 3)), ("3.4.3-dev.1", (3, 4, 3))],
)
def test_parse_panel_version(raw, expected):
    assert parse_panel_version(raw) == expected


def test_legacy_exclusions_stay_legacy_on_32():
    payload = {"uuid": "host", "excludedInternalSquads": ["one"]}
    result = adapt_host_internal_squads_payload(payload, "3.2.0")
    assert result == payload
    assert result is not payload


def test_legacy_exclusions_become_internal_squads_on_34():
    payload = {"uuid": "host", "excludedInternalSquads": ["one"]}
    result = adapt_host_internal_squads_payload(payload, "3.4.3")
    assert result == {
        "uuid": "host",
        "internalSquads": {"mode": "EXCLUDE", "squads": ["one"]},
    }
    assert payload == {"uuid": "host", "excludedInternalSquads": ["one"]}


def test_new_exclude_contract_downgrades_safely_to_32():
    payload = {"uuid": "host", "internalSquads": {"mode": "EXCLUDE", "squads": ["one"]}}
    assert adapt_host_internal_squads_payload(payload, "3.2.0") == {
        "uuid": "host",
        "excludedInternalSquads": ["one"],
    }


def test_allow_only_fails_closed_on_32():
    payload = {"uuid": "host", "internalSquads": {"mode": "ALLOW_ONLY", "squads": ["one"]}}
    with pytest.raises(ValueError, match="require Remnawave 3.4"):
        adapt_host_internal_squads_payload(payload, "3.2.0")


def test_no_squad_field_is_unchanged():
    payload = {"uuid": "host", "remark": "safe"}
    assert adapt_host_internal_squads_payload(payload, "3.4.3") == payload


def test_response_reader_accepts_both_contracts():
    assert read_host_internal_squads({"excludedInternalSquads": ["old"]}) == (
        "EXCLUDE",
        ["old"],
    )
    assert read_host_internal_squads(
        {"internalSquads": {"mode": "ALLOW_ONLY", "squads": ["new"]}}
    ) == ("ALLOW_ONLY", ["new"])


@pytest.mark.asyncio
async def test_api_client_detects_34_from_stats_recap():
    client = object.__new__(RemnawaveApiClient)
    client.get_stats_recap = AsyncMock(return_value={"response": {"version": "3.4.3"}})
    result = await client.adapt_host_payload(
        {"uuid": "host", "excludedInternalSquads": ["one"]}
    )
    assert result["internalSquads"] == {"mode": "EXCLUDE", "squads": ["one"]}
    assert "excludedInternalSquads" not in result
    client.get_stats_recap.assert_awaited_once_with(use_cache=False)


@pytest.mark.asyncio
async def test_api_client_skips_version_probe_without_squad_filter():
    client = object.__new__(RemnawaveApiClient)
    client.get_stats_recap = AsyncMock()
    payload = {"uuid": "host", "remark": "safe"}
    assert await client.adapt_host_payload(payload) == payload
    client.get_stats_recap.assert_not_awaited()
