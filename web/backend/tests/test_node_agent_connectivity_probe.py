"""Security contract for the node agent's bounded connectivity probe."""
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

NODE_AGENT = Path(__file__).resolve().parents[3] / "node-agent"
sys.path.insert(0, str(NODE_AGENT))

from src.command_runner import CommandRunner


@pytest.mark.asyncio
async def test_private_target_is_rejected_without_network_access():
    send = AsyncMock(return_value=True)
    runner = CommandRunner(SimpleNamespace(), send)
    with patch("src.command_runner.asyncio.open_connection", new_callable=AsyncMock) as connect:
        await runner._connectivity_probe({
            "request_id": "r1", "target": "127.0.0.1", "port": 443, "timeout": 3,
        })
    connect.assert_not_awaited()
    assert send.await_args.args[0]["error_code"] == "invalid_target"


@pytest.mark.asyncio
async def test_public_target_uses_tcp_without_shell():
    send = AsyncMock(return_value=True)
    writer = AsyncMock()
    writer.wait_closed = AsyncMock()
    runner = CommandRunner(SimpleNamespace(), send)
    with patch(
        "src.command_runner.asyncio.open_connection",
        new=AsyncMock(return_value=(AsyncMock(), writer)),
    ) as connect:
        await runner._connectivity_probe({
            "request_id": "r2", "target": "1.1.1.1", "port": 443, "timeout": 3,
        })
    connect.assert_awaited_once_with("1.1.1.1", 443)
    assert send.await_args.args[0]["ok"] is True
