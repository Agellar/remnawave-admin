"""Agent Connection Manager — tracks active Agent v2 WebSocket connections.

Singleton that maps node_uuid → WebSocket for the agent command channel.
Thread-safe via asyncio.Lock.
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class AgentConnectionManager:
    """Manages persistent WebSocket connections from Node Agents."""

    def __init__(self):
        self._connections: Dict[str, WebSocket] = {}
        self._pending_requests: Dict[str, tuple[str, asyncio.Future]] = {}
        self._lock = asyncio.Lock()

    async def register(self, node_uuid: str, websocket: WebSocket) -> None:
        """Register a new agent connection."""
        async with self._lock:
            old = self._connections.get(node_uuid)
            if old:
                # Close stale connection
                logger.info("Replacing existing agent connection for %s", node_uuid)
                try:
                    await old.close(code=4000, reason="replaced")
                except Exception as e:
                    logger.debug("Failed to close old agent connection for %s: %s", node_uuid, e)
            self._connections[node_uuid] = websocket
        logger.info("Agent registered: %s (total: %d)", node_uuid, len(self._connections))

    async def unregister(self, node_uuid: str) -> None:
        """Remove agent connection and clean up terminal sessions."""
        async with self._lock:
            self._connections.pop(node_uuid, None)
            disconnected = [
                (request_id, future)
                for request_id, (owner, future) in self._pending_requests.items()
                if owner == node_uuid
            ]
            for request_id, future in disconnected:
                self._pending_requests.pop(request_id, None)
                if not future.done():
                    future.set_exception(ConnectionError("agent_disconnected"))
        logger.info("Agent unregistered: %s (total: %d)", node_uuid, len(self._connections))

        # Close any terminal session associated with this node
        try:
            from web.backend.core.terminal_sessions import terminal_manager
            session = terminal_manager.get_session_for_node(node_uuid)
            if session:
                await terminal_manager.close_session(session.session_id, reason="agent_disconnect")
        except Exception as e:
            logger.debug("Failed to cleanup terminal session on agent unregister: %s", e)

    def is_connected(self, node_uuid: str) -> bool:
        """Check if an agent is connected."""
        return node_uuid in self._connections

    def list_connected(self) -> List[str]:
        """Return list of connected node UUIDs."""
        return list(self._connections.keys())

    @property
    def count(self) -> int:
        return len(self._connections)

    async def send_command(
        self,
        node_uuid: str,
        command: Dict[str, Any],
    ) -> bool:
        """Send a command to a specific agent.

        Returns True if sent successfully, False if agent is not connected.
        """
        ws = self._connections.get(node_uuid)
        if not ws:
            logger.debug("Command dropped: agent %s not connected (cmd_type=%s)", node_uuid, command.get("type", "?"))
            return False

        try:
            await ws.send_text(json.dumps(command, default=str))
            return True
        except Exception as e:
            logger.warning("Failed to send command to %s: %s", node_uuid, e)
            # Connection broken — remove it
            async with self._lock:
                self._connections.pop(node_uuid, None)
            return False

    async def get_websocket(self, node_uuid: str) -> Optional[WebSocket]:
        """Get the WebSocket for a node (for direct streaming like terminal)."""
        return self._connections.get(node_uuid)

    async def request_command(
        self,
        node_uuid: str,
        command: Dict[str, Any],
        *,
        request_id: str,
        timeout: float = 15.0,
    ) -> Dict[str, Any]:
        """Send a bounded request and await its typed agent response."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        async with self._lock:
            if request_id in self._pending_requests:
                raise ValueError("duplicate_request_id")
            self._pending_requests[request_id] = (node_uuid, future)
        try:
            if not await self.send_command(node_uuid, command):
                raise ConnectionError("agent_not_connected")
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            async with self._lock:
                self._pending_requests.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def resolve_request(self, node_uuid: str, message: Dict[str, Any]) -> bool:
        """Resolve a pending typed request from the authenticated node socket."""
        request_id = str(message.get("request_id") or "")
        if not request_id:
            return False
        async with self._lock:
            item = self._pending_requests.get(request_id)
        if not item or item[0] != node_uuid:
            return False
        future = item[1]
        if not future.done():
            future.set_result(message)
        return True


# Global singleton
agent_manager = AgentConnectionManager()
