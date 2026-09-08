"""Процесс работает в UTC независимо от TZ окружения.

asyncpg кодирует naive datetime в timestamptz как локальное время процесса,
а код хранит время как naive UTC: при TZ=Europe/Samara у клиента все
connected_at отставали ровно на четыре часа, и окна «за последний час»
были пусты. Проверяем сам механизм и что connect() его включает.
"""
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from shared.db._base import DatabaseBase, _pin_process_utc

pytestmark = pytest.mark.skipif(not hasattr(time, "tzset"), reason="tzset есть только на POSIX")

SAMARA = "SAM-4"  # POSIX-запись пояса +04:00 — не зависит от tzdata


@pytest.fixture
def samara_process():
    saved = os.environ.get("TZ")
    os.environ["TZ"] = SAMARA
    time.tzset()
    yield
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


def test_naive_datetime_reads_as_utc_after_pin(samara_process):
    naive = datetime(2026, 9, 6, 8, 33)
    # так asyncpg видит naive-метку при TZ≠UTC: 08:33 «по Самаре» = 04:33 UTC
    assert naive.astimezone(timezone.utc).hour == 4
    _pin_process_utc()
    assert naive.astimezone(timezone.utc) == naive.replace(tzinfo=timezone.utc)


async def test_connect_pins_utc_before_creating_pool(samara_process):
    db = DatabaseBase()
    with patch("asyncpg.create_pool", new=AsyncMock(return_value=object())), \
            patch.object(DatabaseBase, "_init_schema", new=AsyncMock()):
        assert await db.connect(database_url="postgresql://x", max_retries=1)
    assert time.strftime("%z") == "+0000"
