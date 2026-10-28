"""Smart reminders: Arabic + English natural language parser, APScheduler background service, Windows Toast."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from cloud_agent.config import logger
from cloud_agent.storage import CloudStorage


@dataclass
class ParsedReminder:
    message: str
    trigger_dt: datetime
    recurrence: str = ""  # "" | "daily" | "hourly"


def show_windows_toast(title: str, message: str) -> None:
    """Display a native Windows 10/11 Toast Notification via PowerShell WinRT APIs."""
    if os.name != "nt":
        return
    safe_title = title.replace("'", "''").replace('"', "")
    safe_msg = message.replace("'", "''").replace('"', "")
    ps_script = f"""
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $textNodes = $template.GetElementsByTagName("text")
    $textNodes.Item(0).AppendChild($template.CreateTextNode('{safe_title}')) > $null
    $textNodes.Item(1).AppendChild($template.CreateTextNode('{safe_msg}')) > $null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Cloud AI Agent')
    $notifier.Show($toast)
    """
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.warning("Toast notification fallback: %s", exc)


def parse_natural_reminder(raw_text: str, now: Optional[datetime] = None) -> ParsedReminder:
    """Parse English and Arabic natural language reminder expressions into (message, trigger_dt, recurrence)."""
    base_now = now or datetime.now()
    text = raw_text.strip()

    # Strip leading command phrases in English & Arabic
    cleaned = re.sub(
        r"^(?:remind me\s+|ذكرني\s+|تذكير\s+)",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    recurrence = ""
    if re.search(r"\b(every\s*day|daily|كل\s*يوم|يوميا|يومياً)\b", cleaned, re.IGNORECASE):
        recurrence = "daily"
        cleaned = re.sub(r"\b(every\s*day|daily|كل\s*يوم|يوميا|يومياً)\b", "", cleaned, flags=re.IGNORECASE).strip()
    elif re.search(r"\b(every\s*hour|hourly|كل\s*ساعة)\b", cleaned, re.IGNORECASE):
        recurrence = "hourly"
        cleaned = re.sub(r"\b(every\s*hour|hourly|كل\s*ساعة)\b", "", cleaned, flags=re.IGNORECASE).strip()

    # 1. Relative durations: "in 10 minutes", "after 2 hours", "بعد 15 دقيقة", "بعد ساعتين"
    rel_en = re.search(
        r"\b(?:in|after)\s+(\d+)\s*(second|seconds|sec|s|minute|minutes|min|m|hour|hours|hr|h)\b",
        cleaned,
        re.IGNORECASE,
    )
    if rel_en:
        val = int(rel_en.group(1))
        unit = rel_en.group(2).lower()
        if unit.startswith("s"):
            delta = timedelta(seconds=val)
        elif unit.startswith("h"):
            delta = timedelta(hours=val)
        else:
            delta = timedelta(minutes=val)
        msg = (cleaned[: rel_en.start()] + " " + cleaned[rel_en.end() :]).strip()
        msg = re.sub(r"^(?:to|ان|أن|بـ?)\s+", "", msg, flags=re.IGNORECASE).strip() or "Reminder"
        return ParsedReminder(message=msg, trigger_dt=base_now + delta, recurrence=recurrence)

    # Arabic dual/relative forms: "بعد دقيقتين", "بعد ساعتين", "بعد 10 دقايق", "بعد 5 ثواني"
    if re.search(r"بعد\s+ساعتين", cleaned):
        msg = re.sub(r"بعد\s+ساعتين", "", cleaned).strip() or "تذكير"
        return ParsedReminder(message=msg, trigger_dt=base_now + timedelta(hours=2), recurrence=recurrence)
    if re.search(r"بعد\s+دقيقتين", cleaned):
        msg = re.sub(r"بعد\s+دقيقتين", "", cleaned).strip() or "تذكير"
        return ParsedReminder(message=msg, trigger_dt=base_now + timedelta(minutes=2), recurrence=recurrence)

    rel_ar = re.search(
        r"بعد\s+(\d+)\s*(ثانية|ثواني|دقيقة|دقايق|دقائق|ساعة|ساعات)",
        cleaned,
    )
    if rel_ar:
        val = int(rel_ar.group(1))
        unit_ar = rel_ar.group(2)
        if "ثان" in unit_ar:
            delta = timedelta(seconds=val)
        elif "ساع" in unit_ar:
            delta = timedelta(hours=val)
        else:
            delta = timedelta(minutes=val)
        msg = (cleaned[: rel_ar.start()] + " " + cleaned[rel_ar.end() :]).strip() or "تذكير"
        return ParsedReminder(message=msg, trigger_dt=base_now + delta, recurrence=recurrence)

    # 2. Day shift: "tomorrow", "بكرة", "غدا", "غداً"
    day_offset = 0
    if re.search(r"\b(tomorrow|بكرة|بكره|غدا|غداً)\b", cleaned, re.IGNORECASE):
        day_offset = 1
        cleaned = re.sub(r"\b(tomorrow|بكرة|بكره|غدا|غداً)\b", "", cleaned, flags=re.IGNORECASE).strip()

    # 3. Time of day: "at 5", "at 5:30 pm", "الساعة 5", "الساعة 9:15 صباحا"
    time_match = re.search(
        r"(?:\bat\b|الساعة|ساعة)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|صباحا|صباحاً|الصبح|مساء|مساءً|المساء|العصر|الليل)?",
        cleaned,
        re.IGNORECASE,
    )
    if time_match and (
        "at" in time_match.group(0).lower()
        or "ساع" in time_match.group(0)
        or time_match.group(2) is not None
        or time_match.group(3) is not None
        or day_offset > 0
        or recurrence != ""
    ):
        hr = int(time_match.group(1))
        mn = int(time_match.group(2) or 0)
        meridiem = (time_match.group(3) or "").lower()

        if meridiem in ("pm", "مساء", "مساءً", "المساء", "العصر", "الليل") and hr < 12:
            hr += 12
        elif meridiem in ("am", "صباحا", "صباحاً", "الصبح") and hr == 12:
            hr = 0
        elif not meridiem and 1 <= hr <= 7:
            # Default "tomorrow at 5" -> 17:00 unless specified
            hr += 12

        hr = max(0, min(23, hr))
        mn = max(0, min(59, mn))
        target_dt = base_now.replace(hour=hr, minute=mn, second=0, microsecond=0) + timedelta(days=day_offset)
        if target_dt <= base_now:
            target_dt += timedelta(days=1)

        msg = (cleaned[: time_match.start()] + " " + cleaned[time_match.end() :]).strip()
        msg = re.sub(r"^(?:to|ان|أن|بـ?)\s+", "", msg, flags=re.IGNORECASE).strip() or "Reminder"
        return ParsedReminder(message=msg, trigger_dt=target_dt, recurrence=recurrence)

    # Default fallback: 10 minutes from now
    msg = re.sub(r"^(?:to|ان|أن)\s+", "", cleaned, flags=re.IGNORECASE).strip() or text
    return ParsedReminder(message=msg, trigger_dt=base_now + timedelta(minutes=10), recurrence=recurrence)


class ReminderService:
    """Background reminder scheduler with SQLite persistence, snooze, and Windows Toast alerts."""

    def __init__(
        self,
        storage: CloudStorage,
        ui_notify_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.storage = storage
        self.ui_notify_callback = ui_notify_callback
        self.scheduler = BackgroundScheduler()
        self._started = False

    def start(self) -> None:
        if not self._started:
            self.scheduler.add_job(self._check_due_reminders, "interval", seconds=5, id="reminder_tick")
            self.scheduler.start()
            self._started = True

    def stop(self) -> None:
        if self._started:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._started = False

    def add_from_natural_language(self, raw_text: str) -> str:
        parsed = parse_natural_reminder(raw_text)
        iso_time = parsed.trigger_dt.strftime("%Y-%m-%d %H:%M:%S")
        rid = self.storage.add_reminder(parsed.message, iso_time, parsed.recurrence)
        rec_tag = f" (recurring: {parsed.recurrence})" if parsed.recurrence else ""
        return f"Reminder #{rid} scheduled for {iso_time}{rec_tag}: {parsed.message}"

    def snooze(self, reminder_id: int, minutes: int = 10) -> str:
        new_dt = (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        ok = self.storage.update_reminder_time(reminder_id, new_dt, status="active")
        if ok:
            return f"Reminder #{reminder_id} snoozed until {new_dt} (+{minutes}m)."
        return f"Reminder #{reminder_id} not found."

    def _check_due_reminders(self) -> None:
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        active_list: List[ dict ] = self.storage.list_reminders(active_only=True)
        for rem in active_list:
            trig = str(rem["trigger_time"])
            if trig <= now_str:
                rid = int(rem["id"])
                msg = str(rem["message"])
                rec = str(rem.get("recurrence") or "")

                show_windows_toast("Cloud Reminder ⏰", msg)
                if self.ui_notify_callback:
                    try:
                        self.ui_notify_callback(f"⏰ Reminder #{rid}: {msg} (Use `/reminders snooze {rid} 10` to snooze)")
                    except Exception:
                        pass

                if rec == "daily":
                    try:
                        dt = datetime.strptime(trig, "%Y-%m-%d %H:%M:%S") + timedelta(days=1)
                    except ValueError:
                        dt = now + timedelta(days=1)
                    self.storage.update_reminder_time(rid, dt.strftime("%Y-%m-%d %H:%M:%S"), status="active")
                elif rec == "hourly":
                    try:
                        dt = datetime.strptime(trig, "%Y-%m-%d %H:%M:%S") + timedelta(hours=1)
                    except ValueError:
                        dt = now + timedelta(hours=1)
                    self.storage.update_reminder_time(rid, dt.strftime("%Y-%m-%d %H:%M:%S"), status="active")
                else:
                    self.storage.mark_reminder_done(rid)
