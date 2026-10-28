"""Google Calendar integration plugin: view, create, and reschedule meetings."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Tuple

import httpx

from cloud_agent.plugins.base import BasePlugin


class GoogleCalendarPlugin(BasePlugin):
    plugin_id = "gcal"
    display_name = "Google Calendar"
    description = "View upcoming events, create new meetings, and reschedule existing meetings."

    def _extract_access_token(self) -> str:
        raw = self.get_token() or ""
        if not raw:
            raise RuntimeError("Google Calendar is not authenticated. Configure OAuth in /integrations.")
        try:
            data = json.loads(raw)
            return str(data.get("access_token") or raw)
        except json.JSONDecodeError:
            return raw.strip()

    async def list_events(self, max_results: int = 10) -> str:
        token = self._extract_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        now_iso = datetime.now(timezone.utc).isoformat()
        url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                url,
                headers=headers,
                params={
                    "timeMin": now_iso,
                    "maxResults": max_results,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
            )
            if resp.status_code >= 400:
                return f"Calendar API error (HTTP {resp.status_code}): {resp.text[:200]}"
            items = resp.json().get("items") or []
            if not items:
                return "No upcoming Google Calendar events."
            lines: List[str] = []
            for ev in items:
                start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
                lines.append(f"- ID: {ev.get('id')} | {start} | {ev.get('summary', '(No Title)')}")
            return "\n".join(lines)

    async def create_event(self, summary: str, start_iso: str, end_iso: str, description: str = "") -> str:
        token = self._extract_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        payload = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_iso},
            "end": {"dateTime": end_iso},
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                return f"Failed to create event (HTTP {resp.status_code}): {resp.text[:200]}"
            data = resp.json()
            return f"Created Calendar event '{summary}' (ID: {data.get('id')}) at {start_iso}."

    async def reschedule_event(self, event_id: str, new_start_iso: str, new_end_iso: str) -> str:
        token = self._extract_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}"
        payload = {
            "start": {"dateTime": new_start_iso},
            "end": {"dateTime": new_end_iso},
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.patch(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                return f"Failed to reschedule event (HTTP {resp.status_code}): {resp.text[:200]}"
            return f"Rescheduled Calendar event {event_id} to {new_start_iso} -> {new_end_iso}."

    def get_tools(self) -> List[Tuple[Dict[str, Any], Callable[..., Awaitable[str]]]]:
        return [
            (
                {
                    "type": "function",
                    "function": {
                        "name": "gcal_list_events",
                        "description": "List upcoming meetings and events from Google Calendar.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "max_results": {"type": "integer", "description": "Number of events to fetch (default 10)."},
                            },
                        },
                    },
                },
                self.list_events,
            ),
            (
                {
                    "type": "function",
                    "function": {
                        "name": "gcal_create_event",
                        "description": "Create a new meeting on Google Calendar.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "summary": {"type": "string", "description": "Meeting title."},
                                "start_iso": {"type": "string", "description": "ISO 8601 start time e.g. 2026-10-29T10:00:00+03:00."},
                                "end_iso": {"type": "string", "description": "ISO 8601 end time e.g. 2026-10-29T11:00:00+03:00."},
                                "description": {"type": "string", "description": "Optional meeting agenda."},
                            },
                            "required": ["summary", "start_iso", "end_iso"],
                        },
                    },
                },
                self.create_event,
            ),
            (
                {
                    "type": "function",
                    "function": {
                        "name": "gcal_reschedule_event",
                        "description": "Reschedule an existing Google Calendar meeting by ID.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "event_id": {"type": "string", "description": "Calendar event ID."},
                                "new_start_iso": {"type": "string", "description": "New ISO 8601 start time."},
                                "new_end_iso": {"type": "string", "description": "New ISO 8601 end time."},
                            },
                            "required": ["event_id", "new_start_iso", "new_end_iso"],
                        },
                    },
                },
                self.reschedule_event,
            ),
        ]
