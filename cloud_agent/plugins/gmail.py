"""Gmail integration plugin: read unread, summarize, draft replies, organize with labels."""

from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from typing import Any, Awaitable, Callable, Dict, List, Tuple

import httpx

from cloud_agent.plugins.base import BasePlugin


class GmailPlugin(BasePlugin):
    plugin_id = "gmail"
    display_name = "Gmail"
    description = "Read, summarize, draft replies, and organize emails with labels."

    def _extract_access_token(self) -> str:
        raw = self.get_token() or ""
        if not raw:
            raise RuntimeError("Gmail is not authenticated. Configure OAuth or access token in /integrations.")
        try:
            data = json.loads(raw)
            return str(data.get("access_token") or raw)
        except json.JSONDecodeError:
            return raw.strip()

    async def list_unread(self, max_results: int = 5) -> str:
        token = self._extract_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers, params={"q": "is:unread", "maxResults": max_results})
            if resp.status_code >= 400:
                return f"Gmail API error (HTTP {resp.status_code}): {resp.text[:200]}"
            messages = resp.json().get("messages") or []
            if not messages:
                return "No unread emails found."
            summaries: List[str] = []
            for m in messages[:max_results]:
                mid = m["id"]
                mresp = await client.get(f"{url}/{mid}", headers=headers, params={"format": "metadata"})
                if mresp.status_code == 200:
                    mdata = mresp.json()
                    hdrs = {
                        h["name"].lower(): h["value"]
                        for h in mdata.get("payload", {}).get("headers", [])
                    }
                    subj = hdrs.get("subject", "(No Subject)")
                    sender = hdrs.get("from", "Unknown")
                    snip = mdata.get("snippet", "")
                    summaries.append(f"- ID: {mid} | From: {sender} | Subject: {subj}\n  Snippet: {snip}")
            return "\n".join(summaries)

    async def create_draft(self, to: str, subject: str, body: str) -> str:
        token = self._extract_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=headers, json={"message": {"raw": encoded}})
            if resp.status_code >= 400:
                return f"Gmail draft creation failed (HTTP {resp.status_code}): {resp.text[:200]}"
            draft_id = resp.json().get("id", "")
            return f"Draft reply created in Gmail (Draft ID: {draft_id}) for {to}."

    async def modify_labels(self, message_id: str, add_labels: List[str] | str = "", remove_labels: List[str] | str = "") -> str:
        token = self._extract_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        add_list = [x.strip() for x in (add_labels.split(",") if isinstance(add_labels, str) else add_labels) if x.strip()]
        rem_list = [x.strip() for x in (remove_labels.split(",") if isinstance(remove_labels, str) else remove_labels) if x.strip()]
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/modify"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                url,
                headers=headers,
                json={"addLabelIds": add_list, "removeLabelIds": rem_list},
            )
            if resp.status_code >= 400:
                return f"Gmail label update failed (HTTP {resp.status_code}): {resp.text[:200]}"
            return f"Updated labels on Gmail message {message_id} (+{add_list} -{rem_list})."

    def get_tools(self) -> List[Tuple[Dict[str, Any], Callable[..., Awaitable[str]]]]:
        return [
            (
                {
                    "type": "function",
                    "function": {
                        "name": "gmail_list_unread",
                        "description": "List and summarize unread emails from Gmail.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "max_results": {"type": "integer", "description": "Max unread emails to fetch (default 5)."},
                            },
                        },
                    },
                },
                self.list_unread,
            ),
            (
                {
                    "type": "function",
                    "function": {
                        "name": "gmail_create_draft",
                        "description": "Create a draft reply email in Gmail.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "to": {"type": "string", "description": "Recipient email address."},
                                "subject": {"type": "string", "description": "Email subject."},
                                "body": {"type": "string", "description": "Draft body content."},
                            },
                            "required": ["to", "subject", "body"],
                        },
                    },
                },
                self.create_draft,
            ),
            (
                {
                    "type": "function",
                    "function": {
                        "name": "gmail_modify_labels",
                        "description": "Organize a Gmail message by adding or removing label IDs (e.g. STARRED, IMPORTANT, UNREAD).",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "message_id": {"type": "string", "description": "Gmail message ID."},
                                "add_labels": {"type": "string", "description": "Comma-separated labels to add."},
                                "remove_labels": {"type": "string", "description": "Comma-separated labels to remove."},
                            },
                            "required": ["message_id"],
                        },
                    },
                },
                self.modify_labels,
            ),
        ]
