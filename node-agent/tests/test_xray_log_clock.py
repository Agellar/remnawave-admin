"""Поправка на часы контейнера Xray.

Xray пишет в access.log локальное время своего контейнера, а агент читал
его как UTC: у ноды с TZ=Europe/Samara подключения уезжали на четыре часа
в будущее. Пояс восстанавливается по mtime файла.

    cd node-agent && python -m pytest
"""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("AGENT_NODE_UUID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
os.environ.setdefault("AGENT_COLLECTOR_URL", "http://collector.test")
os.environ.setdefault("AGENT_AUTH_TOKEN", "token")

from src.collectors.xray_log import (
    XrayLogCollector,
    XrayLogRealtimeCollector,
    _log_utc_offset,
    _parse_lines,
)
from src.config import Settings

NODE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
STAMPED = datetime(2026, 9, 6, 14, 0, 46)  # так Xray пишет при TZ=Europe/Samara
WRITTEN = datetime(2026, 9, 6, 10, 0, 46)  # тот же момент по UTC
SAMARA = timedelta(hours=4)


def epoch(moment):
    return moment.replace(tzinfo=timezone.utc).timestamp()


def line(at, tag="[inbound >> direct]"):
    return "%s from 188.170.87.33:51234 accepted tcp:example.com:443 %s email: 154" % (
        at.strftime("%Y/%m/%d %H:%M:%S.%f"), tag,
    )


class TestLogUtcOffset:
    def test_samara_runs_four_hours_ahead(self):
        assert _log_utc_offset([line(STAMPED)], epoch(WRITTEN)) == SAMARA

    def test_utc_node_needs_nothing(self):
        assert _log_utc_offset([line(WRITTEN)], epoch(WRITTEN)) == timedelta(0)

    def test_western_node_runs_behind(self):
        assert _log_utc_offset([line(WRITTEN - timedelta(hours=5))], epoch(WRITTEN)) == timedelta(hours=-5)

    def test_write_latency_is_rounded_away(self):
        stat_after_write = epoch(WRITTEN + timedelta(seconds=40))
        assert _log_utc_offset([line(STAMPED)], stat_after_write) == SAMARA

    def test_last_readable_line_wins(self):
        lines = [line(STAMPED - timedelta(hours=3)), line(STAMPED), "2026/09/06 14:0"]  # хвост оборван
        assert _log_utc_offset(lines, epoch(WRITTEN)) == SAMARA

    def test_nothing_to_compare(self):
        assert _log_utc_offset(["", "garbage"], epoch(WRITTEN)) is None


class TestParseLinesOffset:
    def test_connection_lands_in_utc(self):
        connections, _, *_ = _parse_lines([line(STAMPED)], NODE, utc_offset=SAMARA)
        assert connections[0].connected_at == WRITTEN

    def test_torrent_event_lands_in_utc(self):
        _, events, *_ = _parse_lines([line(STAMPED, tag="[inbound >> TORRENT]")], NODE, utc_offset=SAMARA)
        assert events[0].detected_at == WRITTEN


def _settings(log, mode):
    return Settings(
        node_uuid=NODE, collector_url="http://collector.test", auth_token="token",
        xray_log_path=str(log), log_parsing_mode=mode,
    )


def _samara_log(tmp_path):
    log = tmp_path / "access.log"
    log.write_text(line(STAMPED) + "\n")
    os.utime(log, (epoch(WRITTEN), epoch(WRITTEN)))
    return log


async def test_polling_collector_corrects_clock(tmp_path):
    log = _samara_log(tmp_path)
    connections = await XrayLogCollector(_settings(log, "polling")).collect()
    assert connections[0].connected_at == WRITTEN


async def test_realtime_collector_corrects_clock(tmp_path):
    log = _samara_log(tmp_path)
    connections = await XrayLogRealtimeCollector(_settings(log, "realtime")).collect()
    assert connections[0].connected_at == WRITTEN
