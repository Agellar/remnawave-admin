"""Pure quorum tests for the continuous Block Radar probes."""
from pathlib import Path
import sys

PLUGIN_SRC = Path(__file__).resolve().parents[3] / "plugins-src"
sys.path.insert(0, str(PLUGIN_SRC))

from rwa_local_block_radar.probes import _locations, summarize


CFG = {"probe_min_ru_results": 2, "probe_confirm_cycles": 2}


def result(source, success, *, country=None, tags=None):
    return {
        "source": source, "success": success, "country": country,
        "tags": tags or [], "vantage_label": "test",
    }


def test_ru_quorum_is_healthy_without_node_probes():
    rows = [
        result("globalping", True, country="RU", tags=["eyeball-network"]),
        result("globalping", True, country="RU", tags=["eyeball-network"]),
        result("globalping", False, country="RU", tags=["eyeball-network"]),
    ]
    summary = summarize(rows, None, CFG, None)
    assert summary["state"] == "healthy"
    assert summary["node_total"] == 0
    assert summary["incident_open"] is False


def test_regional_failure_requires_repeated_confirmation():
    rows = [
        result("globalping", False, country="RU", tags=["eyeball-network"]),
        result("globalping", False, country="RU", tags=["eyeball-network"]),
        result("globalping", True, country="DE", tags=["datacenter-network"]),
    ]
    first = summarize(rows, None, CFG, None)
    assert first["state"] == "regional_suspect"
    assert first["incident_open"] is False
    second = summarize(rows, first, CFG, None)
    assert second["incident_open"] is True


def test_too_few_ru_probes_never_opens_incident():
    rows = [
        result("globalping", False, country="RU", tags=["eyeball-network"]),
    ]
    summary = summarize(rows, {"consecutive_failures": 5}, CFG, None)
    assert summary["state"] == "insufficient"
    assert summary["consecutive_failures"] == 0


def test_ru_and_control_failure_marks_endpoint_down():
    rows = [
        result("globalping", False, country="RU", tags=["eyeball-network"]),
        result("globalping", False, country="RU", tags=["eyeball-network"]),
        result("globalping", False, country="DE", tags=["datacenter-network"]),
    ]
    summary = summarize(rows, None, CFG, None)
    assert summary["state"] == "endpoint_down"
    assert summary["incident_open"] is False


def test_major_live_ru_providers_are_selected():
    probes = [
        {"location": {"country": "RU", "asn": asn}, "tags": ["eyeball-network"]}
        for asn in (8359, 12389, 41786)
    ]
    locations = _locations(probes)
    assert [item.get("asn") for item in locations[:3]] == [8359, 12389, 41786]
