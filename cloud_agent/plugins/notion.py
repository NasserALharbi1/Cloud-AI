"""Notion integration plugin: search, read/write pages, and query databases."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, List, Tuple

import httpx

from cloud_agent.plugins.base import BasePlugin


class NotionPlugin(BasePlugin):
    plugin_id = "notion"
    display_name = "Notion"
    description = "Search workspace, read/write Notion pages, and query databases."

    def _headers(self) -> Dict[str, str]:
        raw = self.get_token() or ""
        if not raw:
            raise RuntimeError("Notion is not authenticated. Set your Notion token/OAuth in /integrations.")
        try:
            data = json.loads(raw)
            token = str(data.get("access_token") or raw)
        except json.JSONDecodeError:
            token = raw.strip()
        return {
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    async def search_notion(self, query: str = "") -> str:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.notion.com/v1/search",
                headers=self._headers(),
                json={"query": query, "page_size": 10},
            )
            if resp.status_code >= 400:
                return f"Notion API error (HTTP {resp.status_code}): {resp.text[:200]}"
            results = resp.json().get("results") or []
            if not results:
                return f"No Notion pages or databases matching '{query}'."
            lines: List[str] = []
            for r in results:
                obj_type = r.get("object", "item")
                rid = r.get("id", "")
                url = r.get("url", "")
                lines.append(f"- [{obj_type.upper()}] ID: {rid} | URL: {url}")
            return "\n".join(lines)

    async def read_page_blocks(self, page_id: str) -> str:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=self._headers(),
            )
            if resp.status_code >= 400:
                return f"Failed to read Notion page (HTTP {resp.status_code}): {resp.text[:200]}"
            blocks = resp.json().get("results") or []
            texts: List[str] = []
            for b in blocks:
                btype = b.get("type", "")
                rich_list = b.get(btype, {}).get("rich_text", [])
                line = "".join(rt.get("plain_text", "") for rt in rich_list)
                if line:
                    texts.append(f"[{btype}] {line}")
            return "\n".join(texts) or "(Page has no text blocks)"

    async def create_page(self, parent_page_id: str, title: str, content: str) -> str:
        payload = {
            "parent": {"page_id": parent_page_id},
            "properties": {
                "title": {
                    "title": [{"text": {"content": title}}],
                }
            },
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": content[:2000]}}],
                    },
                }
            ],
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.notion.com/v1/pages",
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code >= 400:
                return f"Failed creating Notion page (HTTP {resp.status_code}): {resp.text[:200]}"
            data = resp.json()
            return f"Created Notion page '{title}' (ID: {data.get('id')})."

    def get_tools(self) -> List[Tuple[Dict[str, Any], Callable[..., Awaitable[str]]]]:
        return [
            (
                {
                    "type": "function",
                    "function": {
                        "name": "notion_search",
                        "description": "Search pages and databases in Notion.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Search query text."},
                            },
                        },
                    },
                },
                self.search_notion,
            ),
            (
                {
                    "type": "function",
                    "function": {
                        "name": "notion_read_page",
                        "description": "Read text blocks from a Notion page by page_id.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "page_id": {"type": "string", "description": "Notion page ID."},
                            },
                            "required": ["page_id"],
                        },
                    },
                },
                self.read_page_blocks,
            ),
            (
                {
                    "type": "function",
                    "function": {
                        "name": "notion_create_page",
                        "description": "Create a new Notion page under a parent page.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "parent_page_id": {"type": "string", "description": "Parent Notion page ID."},
                                "title": {"type": "string", "description": "Page title."},
                                "content": {"type": "string", "description": "Paragraph body text."},
                            },
                            "required": ["parent_page_id", "title", "content"],
                        },
                    },
                },
                self.create_page,
            ),
        ]
