"""Compatibility helpers for Remnawave API contracts.

Remnawave 3.4 replaced the host field ``excludedInternalSquads`` with
``internalSquads: {mode, squads}``.  Keep the translation in one place so
both the Telegram and web admin clients can talk to 3.2 and 3.4 panels.
"""

from __future__ import annotations

import re
from typing import Any


HOST_INTERNAL_SQUADS_V2 = (3, 4, 0)
HOST_INTERNAL_SQUADS_MODES = frozenset({"EXCLUDE", "ALLOW_ONLY"})


def parse_panel_version(value: Any) -> tuple[int, int, int] | None:
    """Return a comparable three-part version from a Remnawave value."""
    if not isinstance(value, str):
        return None
    match = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def uses_host_internal_squads_v2(panel_version: Any) -> bool:
    """Whether the panel uses the Remnawave 3.4 host-squad contract."""
    parsed = parse_panel_version(panel_version)
    return parsed is not None and parsed >= HOST_INTERNAL_SQUADS_V2


def adapt_host_internal_squads_payload(
    payload: dict[str, Any],
    panel_version: Any,
) -> dict[str, Any]:
    """Translate host squad filters without mutating the caller's payload.

    Legacy callers may provide ``excludedInternalSquads``.  New callers may
    provide ``internalSquads``.  EXCLUDE semantics can be represented on both
    APIs.  ALLOW_ONLY cannot be represented on pre-3.4 panels and therefore
    fails closed instead of silently widening access.
    """
    result = dict(payload)
    new_contract = uses_host_internal_squads_v2(panel_version)

    if new_contract:
        if "internalSquads" not in result and "excludedInternalSquads" in result:
            squads = result.pop("excludedInternalSquads")
            result["internalSquads"] = {"mode": "EXCLUDE", "squads": list(squads or [])}
        return result

    internal_squads = result.pop("internalSquads", None)
    if internal_squads is None:
        return result
    if not isinstance(internal_squads, dict):
        raise ValueError("internalSquads must be an object")

    mode = internal_squads.get("mode", "EXCLUDE")
    if mode not in HOST_INTERNAL_SQUADS_MODES:
        raise ValueError(f"Unsupported internalSquads mode: {mode!r}")
    if mode != "EXCLUDE":
        raise ValueError("ALLOW_ONLY host squads require Remnawave 3.4 or newer")

    result["excludedInternalSquads"] = list(internal_squads.get("squads") or [])
    return result


def read_host_internal_squads(host: dict[str, Any]) -> tuple[str, list[Any]]:
    """Normalize a host response to ``(mode, squads)`` for either API."""
    value = host.get("internalSquads")
    if isinstance(value, dict):
        mode = value.get("mode") or "EXCLUDE"
        squads = value.get("squads")
        return str(mode), list(squads or [])
    return "EXCLUDE", list(host.get("excludedInternalSquads") or [])
