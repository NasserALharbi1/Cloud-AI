"""Base plugin architecture, browser OAuth2 loopback flow, and dynamic plugin loader."""

from __future__ import annotations

import asyncio
import importlib
import json
import pkgutil
import urllib.parse
import webbrowser
from abc import ABC, abstractmethod
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from cloud_agent.config import AppConfig, SecretVault, logger
from cloud_agent.tools import ToolRegistry


class BasePlugin(ABC):
    """Abstract base class for one-file-per-plugin integrations."""

    plugin_id: str = ""
    display_name: str = ""
    description: str = ""

    def get_token(self) -> Optional[str]:
        return SecretVault.get_secret(f"plugin_token:{self.plugin_id}")

    def set_token(self, token_json_or_str: str) -> None:
        SecretVault.set_secret(f"plugin_token:{self.plugin_id}", token_json_or_str)

    def is_authenticated(self) -> bool:
        return bool(self.get_token())

    @abstractmethod
    def get_tools(self) -> List[Tuple[Dict[str, Any], Callable[..., Awaitable[str]]]]:
        """Return list of (openai_tool_schema, async_handler) tuples."""
        raise NotImplementedError


async def run_oauth_browser_flow(
    plugin_id: str,
    auth_endpoint: str,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    scopes: List[str],
    port: int = 8765,
) -> Tuple[bool, str]:
    """Open browser once for OAuth2 consent, capture callback on localhost, and store encrypted token."""
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    captured_code: Dict[str, Optional[str]] = {"code": None, "error": None}

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs:
                captured_code["code"] = qs["code"][0]
                body = b"<html><body style='font-family:sans-serif;background:#111;color:#4ade80;padding:40px;'><h2>Cloud OAuth Complete</h2><p>Authentication succeeded. You may close this window and return to Cloud.</p></body></html>"
            else:
                captured_code["error"] = qs.get("error", ["unknown_error"])[0]
                body = b"<html><body style='font-family:sans-serif;background:#111;color:#f87171;padding:40px;'><h2>OAuth Error</h2><p>Could not complete OAuth flow.</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
    }
    full_auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(params)}"

    def _wait_for_code() -> None:
        server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
        server.timeout = 120
        server.handle_request()
        server.server_close()

    webbrowser.open(full_auth_url)
    await asyncio.to_thread(_wait_for_code)

    if captured_code["error"]:
        return False, f"OAuth error: {captured_code['error']}"
    if not captured_code["code"]:
        return False, "OAuth timed out waiting for browser callback."

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            token_endpoint,
            data={
                "code": captured_code["code"],
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 400:
            return False, f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:160]}"
        token_data = resp.json()
        SecretVault.set_secret(f"plugin_token:{plugin_id}", json.dumps(token_data))
        return True, "OAuth token acquired and encrypted via Windows DPAPI."


class PluginManager:
    """Discovers plugins in cloud_agent/plugins/ and binds enabled ones to ToolRegistry."""

    def __init__(self, config: AppConfig, tool_registry: ToolRegistry) -> None:
        self.config = config
        self.tool_registry = tool_registry
        self.plugins: Dict[str, BasePlugin] = {}
        self.discover_plugins()

    def discover_plugins(self) -> None:
        """Auto-discover all BasePlugin subclasses in cloud_agent.plugins (supports both source & PyInstaller exe)."""
        builtin_modules = ["gmail", "gcal", "notion"]
        pkg_dir = Path(__file__).parent
        discovered_names = set(builtin_modules)
        for _, module_name, _ in pkgutil.iter_modules([str(pkg_dir)]):
            if module_name not in ("base", "mcp_client", "__init__"):
                discovered_names.add(module_name)

        for module_name in sorted(discovered_names):
            try:
                mod = importlib.import_module(f"cloud_agent.plugins.{module_name}")
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BasePlugin)
                        and attr is not BasePlugin
                        and getattr(attr, "plugin_id", "")
                    ):
                        instance = attr()
                        self.plugins[instance.plugin_id] = instance
            except Exception as exc:
                logger.error("Failed loading plugin module %s: %s", module_name, exc)

    def sync_enabled_tools(self) -> None:
        """Register tools from all enabled plugins into ToolRegistry."""
        self.tool_registry.clear_external_tools()
        for pid in self.config.enabled_integrations:
            plugin = self.plugins.get(pid)
            if plugin:
                for schema, handler in plugin.get_tools():
                    self.tool_registry.register_external_tool(schema, handler)

    def toggle_plugin(self, plugin_id: str, enable: Optional[bool] = None) -> bool:
        if plugin_id not in self.plugins:
            return False
        currently_on = plugin_id in self.config.enabled_integrations
        new_state = (not currently_on) if enable is None else enable
        if new_state and not currently_on:
            self.config.enabled_integrations.append(plugin_id)
        elif not new_state and currently_on:
            self.config.enabled_integrations.remove(plugin_id)
        self.config.save()
        self.sync_enabled_tools()
        return new_state
