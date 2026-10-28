"""Model Context Protocol (MCP) JSON-RPC 2.0 client for stdio MCP servers."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from cloud_agent.config import AppConfig, logger
from cloud_agent.tools import ToolRegistry


class MCPStdioServerConnection:
    """Manages a single stdio MCP server process over JSON-RPC 2.0."""

    def __init__(self, name: str, command: str, args: List[str], env: Optional[Dict[str, str]] = None) -> None:
        self.name = name
        self.command = command
        self.args = args
        self.env = env
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._req_id = 0
        self._lock = asyncio.Lock()

    async def start_and_initialize(self) -> bool:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            resp = await self._send_rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "cloud-ai-agent", "version": "1.0.0"},
                },
            )
            if resp is not None:
                await self._send_notification("notifications/initialized", {})
                return True
        except Exception as exc:
            logger.error("Failed to start MCP server %s: %s", self.name, exc)
        return False

    async def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            return
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
        self._proc.stdin.write(msg.encode("utf-8"))
        await self._proc.stdin.drain()

    async def _send_rpc(self, method: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with self._lock:
            if not self._proc or not self._proc.stdin or not self._proc.stdout:
                return None
            self._req_id += 1
            rid = self._req_id
            req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            self._proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
            try:
                while True:
                    line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=15.0)
                    if not line:
                        return None
                    data = json.loads(line.decode("utf-8", errors="replace"))
                    if data.get("id") == rid:
                        return data.get("result")
            except Exception as exc:
                logger.warning("MCP RPC %s timed out or failed: %s", method, exc)
                return None

    async def list_tools(self) -> List[Dict[str, Any]]:
        res = await self._send_rpc("tools/list", {})
        if not res:
            return []
        return list(res.get("tools") or [])

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        res = await self._send_rpc("tools/call", {"name": tool_name, "arguments": arguments})
        if not res:
            return f"MCP server '{self.name}' returned no response for '{tool_name}'."
        content_list = res.get("content") or []
        parts = [c.get("text", str(c)) for c in content_list]
        return "\n".join(parts) if parts else json.dumps(res)

    async def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except Exception:
                pass


class MCPManager:
    """Connects configured MCP servers from AppConfig and exposes their tools to the agent."""

    def __init__(self, config: AppConfig, tool_registry: ToolRegistry) -> None:
        self.config = config
        self.tool_registry = tool_registry
        self.connections: Dict[str, MCPStdioServerConnection] = {}

    async def connect_all(self) -> List[str]:
        status_msgs: List[str] = []
        for entry in self.config.mcp_servers:
            name = entry.get("name", "mcp")
            cmd = entry.get("command", "")
            args = list(entry.get("args", []))
            if not cmd:
                continue
            conn = MCPStdioServerConnection(name, cmd, args)
            ok = await conn.start_and_initialize()
            if ok:
                self.connections[name] = conn
                tools = await conn.list_tools()
                for t in tools:
                    tname = f"mcp_{name}_{t.get('name', 'tool')}"
                    orig_name = t.get("name", "")
                    schema = {
                        "type": "function",
                        "function": {
                            "name": tname,
                            "description": f"[MCP:{name}] {t.get('description', '')}",
                            "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                        },
                    }

                    def _make_handler(c: MCPStdioServerConnection, oname: str) -> Callable[..., Any]:
                        async def _handler(**kwargs: Any) -> str:
                            return await c.call_tool(oname, kwargs)
                        return _handler

                    self.tool_registry.register_external_tool(schema, _make_handler(conn, orig_name))
                status_msgs.append(f"Connected MCP '{name}' ({len(tools)} tools)")
            else:
                status_msgs.append(f"Failed to connect MCP '{name}'")
        return status_msgs

    async def shutdown_all(self) -> None:
        for conn in self.connections.values():
            await conn.stop()
        self.connections.clear()
