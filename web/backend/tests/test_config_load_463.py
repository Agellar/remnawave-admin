"""An unknown persisted type must not discard otherwise healthy config."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.config_service import ConfigValueType, DEFAULT_CONFIG_DEFINITIONS, DynamicConfigService


def test_default_value_types_are_all_valid():
    for definition in DEFAULT_CONFIG_DEFINITIONS:
        ConfigValueType(definition["value_type"])


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_type", ["str", "unknown", None])
async def test_unknown_type_does_not_abort_later_settings(legacy_type):
    def row(key, value, value_type):
        return {
            "key": key, "value": value, "value_type": value_type,
            "category": "violations", "subcategory": None, "display_name": key,
            "description": None, "default_value": value, "env_var_name": None,
            "is_secret": False, "is_readonly": False, "validation_regex": None,
            "options_json": None, "sort_order": 0, "created_at": None, "updated_at": None,
        }
    conn = SimpleNamespace(fetch=AsyncMock(return_value=[
        row("first", "3", "int"), row("legacy", "allowed", legacy_type), row("last", "5", "int"),
    ]))

    @asynccontextmanager
    async def acquire():
        yield conn

    with patch("shared.config_service.db_service", SimpleNamespace(is_connected=True, acquire=acquire)):
        service = DynamicConfigService()
        await service._load_all_from_db()
    assert service.get("first") == 3
    assert service.get("last") == 5
    assert service.get("legacy") == "allowed"
    assert service._cache["legacy"].value_type == ConfigValueType.STRING
