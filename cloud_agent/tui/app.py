"""Minimal dark TUI for Cloud AI Agent with animated color-shifting start screen."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Label, Markdown, Static
from textual.worker import Worker

from cloud_agent.agent import CloudAgent, load_cloud_md_instructions
from cloud_agent.config import AppConfig, get_windows_clipboard
from cloud_agent.memory import MemoryEngine
from cloud_agent.plugins.base import PluginManager
from cloud_agent.plugins.mcp_client import MCPManager
from cloud_agent.providers import ProviderManager
from cloud_agent.reminders import ReminderService
from cloud_agent.rtl import (
    check_terminal_arabic_font_support,
    contains_arabic,
    detect_direction,
    format_bidirectional_text,
    shape_rtl_line,
)
from cloud_agent.storage import CloudStorage
from cloud_agent.tools import ToolExecutionResult, ToolRegistry
from cloud_agent.tui.modals import (
    ApprovalModal,
    CommandPaletteModal,
    FileAutocompleteModal,
    IntegrationsModal,
    ModelPickerModal,
    ProviderAddModal,
    SettingsModal,
)

# 3D intensity map for the '@' Cloud / Crown-Cloud shape:
# ' ' = empty, '.' = wisp edge (animates in/out), '1' = dark edge '@', '2' = mid '@', '3' = bright core '@'
CLOUD_SHAPE_MAP: List[str] = [
    "            .11.        .1221.        .11.            ",
    "          .123321.    .12333321.    .123321.          ",
    "         123333321.  123333333321  .123333321         ",
    "       .12333333321112333333333321112333333321.       ",
    "      .1233333333333333333333333333333333333321.      ",
    "     .123333333333333333333333333333333333333321.     ",
    "     12233333333333333333333333333333333333333221     ",
    "      122233333333333333333333333333333333332221      ",
    "       1122223333333333333333333333333333222211       ",
    "         .1122222222222222222222222222222211.         ",
]

# Smooth color-wave palettes (bright core, mid contour, dark 3D edge)
WAVE_PALETTE_CORE = [
    "#5fd1a0", "#34d399", "#2dd4bf", "#38bdf8",
    "#60a5fa", "#818cf8", "#a78bfa", "#6ee7b7",
]
WAVE_PALETTE_MID = [
    "#3d9e74", "#258564", "#1f8a7c", "#2584b0",
    "#3e6fb3", "#575fb3", "#735cb0", "#47a380",
]
WAVE_PALETTE_EDGE = [
    "#235943", "#194f3e", "#154f48", "#184b63",
    "#234169", "#323769", "#423566", "#295e4a",
]


def render_animated_cloud_frame(tick: int) -> Tuple[str, str]:
    """Generate one frame of the floating, color-shifting '@' cloud and 'C L O U D' title."""
    phase = tick * 0.28
    # Gentle horizontal floating motion (-2 to +2 spaces)
    drift = int(round(math.sin(tick * 0.18) * 2))
    pad_left = " " * max(0, drift + 2)

    n_colors = len(WAVE_PALETTE_CORE)
    lines: List[str] = []

    for r_idx, row_str in enumerate(CLOUD_SHAPE_MAP):
        row_out: List[str] = [pad_left]
        for c_idx, ch in enumerate(row_str):
            if ch == " ":
                row_out.append(" ")
                continue
            # Animated puff wisps at '.' positions
            if ch == ".":
                wisp_val = math.sin(c_idx * 0.7 + r_idx * 1.1 - phase * 1.3)
                if wisp_val > 0.35:
                    idx = int(math.floor(c_idx * 0.15 + r_idx * 0.3 - phase)) % n_colors
                    row_out.append(f"[{WAVE_PALETTE_EDGE[idx]}]@[/]")
                else:
                    row_out.append(" ")
                continue

            wave_pos = (c_idx * 0.14) + (r_idx * 0.35) - phase
            color_idx = int(math.floor(wave_pos)) % n_colors
            if ch == "3":
                col_hex = WAVE_PALETTE_CORE[color_idx]
            elif ch == "2":
                col_hex = WAVE_PALETTE_MID[color_idx]
            else:
                col_hex = WAVE_PALETTE_EDGE[color_idx]
            row_out.append(f"[{col_hex}]@[/]")
        lines.append("".join(row_out))

    # Shimmering 'C L O U D' title in sync with the wave
    title_chars = ["C", "L", "O", "U", "D"]
    title_parts: List[str] = []
    for i, tch in enumerate(title_chars):
        cidx = int(math.floor(i * 0.8 - phase)) % n_colors
        title_parts.append(f"[bold {WAVE_PALETTE_CORE[cidx]}]{tch}[/]")
    spaced_title = "  ".join(title_parts)

    return "\n".join(lines), spaced_title


EN_TO_AR_KEYMAP: Dict[str, str] = {
    "q": "ض", "w": "ص", "e": "ث", "r": "ق", "t": "ف", "y": "غ", "u": "ع", "i": "ه", "o": "خ", "p": "ح",
    "[": "ج", "]": "د", "a": "ش", "s": "س", "d": "ي", "f": "ب", "g": "ل", "h": "ا", "j": "ت", "k": "ن",
    "l": "م", ";": "ك", "'": "ط", "z": "ئ", "x": "ء", "c": "ؤ", "v": "ر", "b": "لا", "n": "ى", "m": "ة",
    ",": "و", ".": "ز", "/": "ظ", "`": "ذ",
    "Q": "َ", "W": "ً", "E": "ُ", "R": "ٌ", "T": "لإ", "Y": "إ", "U": "‘", "I": "÷", "O": "×", "P": "؛",
    "A": "ِ", "S": "ٍ", "D": "]", "F": "[", "G": "لأ", "H": "أ", "J": "ـ", "K": "،", "L": "/", "Z": "~",
    "X": "ْ", "C": "}", "V": "{", "B": "لآ", "N": "آ", "M": "’", "<": ",", ">": ".", "?": "؟",
}


class ToolCallItem(Vertical):
    """Single collapsed tool-call line ('▸ read_file report.csv   done · 0.2s'), expandable on Enter."""

    BINDINGS = [Binding("enter", "toggle_expand", "Expand/Collapse", show=False)]

    def __init__(self, summary_text: str, details_text: str) -> None:
        super().__init__(classes="tool-call-block")
        self.summary_text = summary_text
        self.details_text = details_text
        self._expanded = False

    def compose(self) -> ComposeResult:
        hdr = Static(f"[#5fd1a0]▸[/] {self.summary_text}", classes="tool-call-header")
        hdr.can_focus = True
        yield hdr
        yield Static(self.details_text, markup=False, classes="tool-call-details")

    def action_toggle_expand(self) -> None:
        self._expanded = not self._expanded
        hdr = self.query_one(".tool-call-header", Static)
        details = self.query_one(".tool-call-details", Static)
        icon = "▾" if self._expanded else "▸"
        hdr.update(f"[#5fd1a0]{icon}[/] {self.summary_text}")
        details.styles.display = "block" if self._expanded else "none"

    def on_click(self, _event: events.Click) -> None:
        self.action_toggle_expand()


class CloudInput(Input):
    """Prompt input with Arabic shaping on render, F2 Arabic keyboard map, and multi-line height growth up to 6 lines."""

    async def _on_key(self, event: events.Key) -> None:
        app_ref = self.app
        if getattr(app_ref, "_arabic_kbd_mode", False):
            ch = event.character
            if ch and len(ch) == 1 and ch in EN_TO_AR_KEYMAP:
                if ch == "/" and self.value == "":
                    await super()._on_key(event)
                    return
                ar_char = EN_TO_AR_KEYMAP[ch]
                start, end = self.selection
                self.replace(ar_char, start, end)
                event.prevent_default()
                event.stop()
                return
        await super()._on_key(event)

    def render_line(self, y: int) -> Any:
        orig_val = self.value
        if orig_val and contains_arabic(orig_val):
            shaped = shape_rtl_line(orig_val, enable_shaping=True)
            if len(shaped) == len(orig_val):
                self._value = shaped
                try:
                    return super().render_line(y)
                finally:
                    self._value = orig_val
        return super().render_line(y)


class CloudApp(App[None]):
    """Minimal dark TUI for Cloud AI Agent with animated start screen."""

    CSS_PATH = "styles.tcss"
    TITLE = "Cloud"

    BINDINGS = [
        Binding("f2", "toggle_arabic_kbd", "Arabic Toggle (F2)", priority=True, show=False),
        Binding("f3", "open_model_picker", "Models (F3)", show=False),
        Binding("shift+tab", "toggle_auto_approve", "Auto-Approve (Shift+Tab)", priority=True, show=False),
        Binding("shift+insert", "paste_clipboard", "Paste (Shift+Insert)", priority=True, show=False),
        Binding("ctrl+c", "cancel_task", "Cancel Task (Ctrl+C)", priority=True, show=False),
    ]

    def __init__(self, initial_cwd: Optional[Path] = None) -> None:
        super().__init__()
        self.cwd = (initial_cwd or Path.cwd()).resolve()
        self.config_data = AppConfig.load()
        self.storage = CloudStorage()
        self.provider_mgr = ProviderManager(self.config_data, self.storage)
        self.reminder_service = ReminderService(
            self.storage,
            ui_notify_callback=lambda msg: self.call_from_thread(self._post_system_notice, msg),
        )
        self.tool_registry = ToolRegistry(
            self.storage,
            self.cwd,
            reminder_callback=self._handle_tool_reminder,
        )
        self.plugin_mgr = PluginManager(self.config_data, self.tool_registry)
        self.plugin_mgr.sync_enabled_tools()
        self.mcp_mgr = MCPManager(self.config_data, self.tool_registry)
        self.memory_engine = MemoryEngine(self.storage)
        self.agent = CloudAgent(
            self.config_data,
            self.storage,
            self.provider_mgr,
            self.tool_registry,
            self.memory_engine,
        )
        self._in_chat_view = False
        self._arabic_kbd_mode = False
        self._font_hint_shown = False
        self._anim_tick = 0
        self._active_worker: Optional[Worker[Any]] = None
        self._transcript_log: List[str] = []

    @property
    def clipboard(self) -> str:
        """Bridge Textual internal clipboard to the Windows OS Clipboard for Ctrl+V."""
        win_clip = get_windows_clipboard()
        if win_clip:
            return win_clip
        return getattr(self, "_clipboard", "")

    def action_paste_clipboard(self) -> None:
        clip_text = get_windows_clipboard()
        if not clip_text:
            return
        focused = self.focused
        if isinstance(focused, Input):
            start, end = focused.selection
            focused.replace(clip_text, start, end)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:
            self.action_paste_clipboard()
            event.stop()

    def action_toggle_arabic_kbd(self) -> None:
        self._arabic_kbd_mode = not self._arabic_kbd_mode
        self._refresh_chrome()

    def compose(self) -> ComposeResult:
        cloud_frame0, title_frame0 = render_animated_cloud_frame(0)
        with Vertical(id="main-root"):
            # 1. Slim 1-line header (shown in chat view)
            with Horizontal(id="slim-header"):
                yield Label("", id="header-left")
                yield Label("", id="header-right")

            # Centered Start Screen (matches screenshot layout with animated cloud)
            with Center(id="start-hero"):
                with Vertical(id="hero-box"):
                    yield Static(cloud_frame0, id="logo-art")
                    yield Label(title_frame0, id="spaced-title")
                    yield Label("What can I do for you?", id="subtitle-bold")
                    yield Label("/ commands · @ files · F3 models", id="hint-dim")
                    with Horizontal(id="hero-input-row"):
                        yield Label("> ", id="hero-prompt-symbol")
                        yield CloudInput(
                            placeholder="Ask for a change, or / for commands",
                            id="hero-prompt-input",
                        )
                    yield Label("", id="hero-counters-row")
                    yield Label("", id="hero-model-row")

            # 2. Borderless message area with generous spacing (shown after first message)
            yield VerticalScroll(id="chat-scroll")

            # 3. Thin divider above input + 4. Slim 1-line footer (shown in chat view)
            with Vertical(id="bottom-dock"):
                with Horizontal(id="input-row"):
                    yield Label("❯ ", id="prompt-symbol")
                    yield CloudInput(
                        placeholder="Ask for a change, or / for commands",
                        id="prompt-input",
                    )
                with Horizontal(id="slim-footer"):
                    yield Label("", id="footer-left")
                    yield Label("", id="footer-right")

    async def on_mount(self) -> None:
        self.reminder_service.start()
        self._refresh_chrome()
        self.query_one("#hero-prompt-input", Input).focus()
        self.set_interval(0.085, self._tick_cloud_animation)
        self._sync_live_usage()
        if self.config_data.mcp_servers:
            self._init_mcp_background()

    def _tick_cloud_animation(self) -> None:
        """Animate the floating, color-changing '@' cloud on the start screen."""
        if self._in_chat_view:
            return
        self._anim_tick += 1
        cloud_art, title_art = render_animated_cloud_frame(self._anim_tick)
        self.query_one("#logo-art", Static).update(cloud_art)
        self.query_one("#spaced-title", Label).update(title_art)

    async def on_unmount(self) -> None:
        self.reminder_service.stop()
        await self.mcp_mgr.shutdown_all()

    @work(exclusive=False)
    async def _sync_live_usage(self) -> None:
        await self.provider_mgr.refresh_live_usage()
        self._refresh_chrome()

    def _check_terminal_font_once(self) -> None:
        if self._font_hint_shown:
            return
        self._font_hint_shown = True
        supports_ar, font_name = check_terminal_arabic_font_support()
        if not supports_ar:
            self._post_system_notice(
                f"Terminal font ({font_name}) may not contain Arabic glyphs. "
                "For native Arabic rendering, use Windows Terminal with an Arabic font or run 'cloud --gui'."
            )

    @work(exclusive=False)
    async def _init_mcp_background(self) -> None:
        msgs = await self.mcp_mgr.connect_all()
        for m in msgs:
            self._post_system_notice(m)

    def _handle_tool_reminder(self, action: str, text: str) -> str:
        if action == "add":
            return self.reminder_service.add_from_natural_language(text)
        return ""

    # ── Start Screen & Chat View Chrome Refresh ───────────────────────────────

    def _get_active_model_label(self) -> str:
        if self.config_data.active_provider and self.config_data.active_model:
            return f"{self.config_data.active_provider}/{self.config_data.active_model}"
        return "no-model"

    def _refresh_chrome(self) -> None:
        model_str = self._get_active_model_label()
        quota_str = self.provider_mgr.get_active_quota_bar()
        free_cnt, unavail_cnt, reqs_today = self.provider_mgr.get_status_counters()

        # Start screen counters & active model line
        hero_counters = self.query_one("#hero-counters-row", Label)
        hero_counters.update(
            f"[#7f8b87]Free Models:[/] [#d7dedb]{free_cnt}[/]          "
            f"[#7f8b87]Unavailable:[/] [#d7dedb]{unavail_cnt}[/]          "
            f"[#7f8b87]Requests Today:[/] [#d7dedb]{reqs_today}[/]"
        )

        ar_state = "[#5fd1a0]F2 Arabic:ON[/]" if self._arabic_kbd_mode else "F2 Arabic"
        auto_state = (
            "[#5fd1a0]auto-approve on[/]"
            if self.config_data.auto_approve
            else "auto-approve off"
        )

        hero_model = self.query_one("#hero-model-row", Label)
        hero_model.update(
            f"[#7ab8ff]{model_str}[/]  [#5fd1a0]{quota_str}[/]  ·  [#7f8b87]{ar_state}  ·  Agent · {auto_state}[/]"
        )

        # 1. Slim Header (in chat view)
        hdr_left = self.query_one("#header-left", Label)
        hdr_right = self.query_one("#header-right", Label)
        hdr_left.update("[bold #5fd1a0]C L O U D[/]  [#7f8b87]v1.0[/]")
        hdr_right.update(f"[#7ab8ff]{model_str}[/]  [#5fd1a0]{quota_str}[/]  [#7f8b87]F3 models[/]")

        # 4. Slim Footer (in chat view)
        cwd_display = str(self.cwd)
        if len(cwd_display) > 36:
            cwd_display = "..." + cwd_display[-33:]

        ftr_left = self.query_one("#footer-left", Label)
        ftr_right = self.query_one("#footer-right", Label)
        ftr_left.update(f"[#7f8b87]{model_str} · {cwd_display}[/]")
        ftr_right.update(
            f"[#7f8b87]/ commands · @ files · {ar_state} · Agent · {auto_state}[/]"
        )

    def _active_input_widget(self) -> Input:
        if self._in_chat_view:
            return self.query_one("#prompt-input", Input)
        return self.query_one("#hero-prompt-input", Input)

    def _switch_to_chat_view(self) -> None:
        if not self._in_chat_view:
            self._in_chat_view = True
            self.query_one("#start-hero").styles.display = "none"
            self.query_one("#slim-header").styles.display = "block"
            self.query_one("#chat-scroll").styles.display = "block"
            self.query_one("#bottom-dock").styles.display = "block"
            self.query_one("#prompt-input", Input).focus()
            self._check_terminal_font_once()

    def _post_system_notice(self, text: str) -> None:
        """Render a single dim amber line starting with '↻'."""
        self._switch_to_chat_view()
        chat_scroll = self.query_one("#chat-scroll", VerticalScroll)
        clean_txt = text.lstrip("•↻ ").strip()
        shaped = format_bidirectional_text(clean_txt, enable_shaping=self.config_data.rtl_shaping)
        chat_scroll.mount(Static(f"[#e5b567]↻ {shaped}[/]", classes="system-notice-line"))
        chat_scroll.scroll_end(animate=False)
        self._refresh_chrome()

    # ── Key Actions & Input Handling ──────────────────────────────────────────

    def action_open_model_picker(self) -> None:
        def _after_pick(result: Optional[str]) -> None:
            if result == "__add_provider__":
                self.push_screen(ProviderAddModal(self.provider_mgr), self._after_modal_notice)
            elif result and self._in_chat_view:
                self._post_system_notice(result)
            self._refresh_chrome()
            self._active_input_widget().focus()

        self.push_screen(ModelPickerModal(self.provider_mgr), _after_pick)

    def _after_modal_notice(self, result: Optional[str]) -> None:
        if result and self._in_chat_view:
            self._post_system_notice(result)
        self._refresh_chrome()
        self._active_input_widget().focus()

    def action_toggle_auto_approve(self) -> None:
        self.config_data.auto_approve = not self.config_data.auto_approve
        self.config_data.save()
        self._refresh_chrome()

    def action_cancel_task(self) -> None:
        if self._active_worker is not None and self._active_worker.is_running:
            self._active_worker.cancel()
            self._post_system_notice("Task cancelled.")

    @on(Input.Changed, "#hero-prompt-input, #prompt-input")
    def _on_prompt_changed(self, event: Input.Changed) -> None:
        val = event.value
        if event.input.id == "prompt-input":
            term_w = max(40, self.size.width - 8)
            est_lines = max(1, min(6, (len(val) // term_w) + val.count("\n") + 1))
            self.query_one("#input-row").styles.height = est_lines
            event.input.styles.height = est_lines

        target_input = event.input
        if val == "/":
            def _on_cmd_chosen(chosen: Optional[str]) -> None:
                if chosen:
                    if chosen in ("/provider add", "/provider remove", "/cwd", "/forget", "/reminders", "/tasks"):
                        target_input.value = chosen + " "
                        target_input.cursor_position = len(target_input.value)
                        target_input.focus()
                    else:
                        target_input.value = ""
                        self._dispatch_slash_command(chosen)
                else:
                    target_input.focus()

            self.push_screen(CommandPaletteModal(), _on_cmd_chosen)
        elif val.endswith("@") and (len(val) == 1 or val[-2].isspace()):
            def _on_file_chosen(chosen_file: Optional[str]) -> None:
                if chosen_file:
                    target_input.value = val[:-1] + chosen_file
                    target_input.cursor_position = len(target_input.value)
                target_input.focus()

            self.push_screen(FileAutocompleteModal(self.cwd), _on_file_chosen)

    @on(Input.Submitted, "#hero-prompt-input, #prompt-input")
    def _on_prompt_submitted(self, event: Input.Submitted) -> None:
        raw_text = event.value.strip()
        event.input.value = ""
        if event.input.id == "prompt-input":
            self.query_one("#input-row").styles.height = 1
            event.input.styles.height = 1
        if not raw_text:
            return
        if raw_text.startswith("/"):
            self._dispatch_slash_command(raw_text)
        else:
            self._start_agent_turn(raw_text)

    # ── Slash Command Dispatcher ──────────────────────────────────────────────

    def _dispatch_slash_command(self, cmd_line: str) -> None:
        parts = cmd_line.strip().split(maxsplit=2)
        cmd = parts[0].lower()
        arg1 = parts[1] if len(parts) > 1 else ""
        arg2 = parts[2] if len(parts) > 2 else ""

        if cmd == "/help":
            help_md = (
                "### Commands & Keys\n"
                "- `/help` — Command reference\n"
                "- `/usage` — Check live remaining usage and daily limits\n"
                "- `/cloudmd` — View active custom instructions from `cloud.md`\n"
                "- `/model` (`F3`) — Switch model and check status\n"
                "- `/provider add|list|remove` — Manage providers and API keys\n"
                "- `/integrations` — Configure Gmail, Google Calendar, Notion, and MCP\n"
                "- `/memory` / `/forget` — View or delete stored memories\n"
                "- `/reminders` — Manage natural-language reminders\n"
                "- `/tasks` — Manage personal tasks\n"
                "- `/clear` — Clear chat transcript\n"
                "- `/cwd [path]` — View or change working directory\n"
                "- `/auto` (`Shift+Tab`) — Toggle auto-approve\n"
                "- `/export [file.md]` — Export transcript\n"
                "- `/settings` — Open preferences\n"
                "- `/exit` — Quit Cloud\n"
                "- `F2` — Toggle Arabic/English keyboard input"
            )
            self._post_markdown_reply(help_md)

        elif cmd == "/usage":
            self._run_usage_command()

        elif cmd == "/cloudmd":
            txt, path = load_cloud_md_instructions(self.cwd)
            if path and txt:
                self._post_markdown_reply(f"### Active `cloud.md` (`{path}`)\n\n{txt}")
            else:
                self._post_system_notice("No cloud.md found. Create 'cloud.md' in the project folder to customize persona/rules.")

        elif cmd == "/model":
            self.action_open_model_picker()

        elif cmd == "/provider":
            sub = arg1.lower() or "list"
            if sub == "add":
                preset_name = arg2.strip() if arg2 else "openai"
                self.push_screen(ProviderAddModal(self.provider_mgr, initial_name=preset_name), self._after_modal_notice)
            elif sub == "remove" and arg2:
                ok = self.provider_mgr.remove_provider(arg2)
                self._post_system_notice(f"Removed provider '{arg2}'." if ok else f"Provider '{arg2}' not found.")
            else:
                models = self.provider_mgr.get_all_models()
                if not models:
                    self._post_system_notice("No providers configured. Run /provider add.")
                else:
                    lines = ["### Providers & Models"]
                    for m in models:
                        act = "(active)" if m["is_active"] else ""
                        lines.append(
                            f"- `{m['provider']}/{m['model']}` · {m['status']} · {m['quota']} · today: {m['requests_today']} {act}"
                        )
                    self._post_markdown_reply("\n".join(lines))

        elif cmd == "/integrations":
            self.push_screen(IntegrationsModal(self.config_data, self.plugin_mgr), self._after_modal_notice)

        elif cmd == "/memory":
            if arg1.lower() == "add" and arg2:
                mid = self.storage.add_memory("fact", arg2)
                self._post_system_notice(f"Memory #{mid} saved: {arg2}")
            else:
                mems = self.storage.list_memories()
                if not mems:
                    self._post_system_notice("No stored memories yet.")
                else:
                    lines = ["### Memories"]
                    for m in mems:
                        lines.append(f"- `#{m['id']}` [{m['category']}] {m['content']}")
                    self._post_markdown_reply("\n".join(lines))

        elif cmd == "/forget":
            target = (arg1 + " " + arg2).strip()
            if not target:
                self._post_system_notice("Usage: /forget <id | query | all>")
            else:
                deleted = self.storage.delete_memory(target)
                self._post_system_notice(f"Deleted {deleted} memory item(s).")

        elif cmd == "/reminders":
            sub = arg1.lower()
            if sub == "add" and arg2:
                self._post_system_notice(self.reminder_service.add_from_natural_language(arg2))
            elif sub == "snooze" and arg2:
                sp = arg2.split()
                rid = int(sp[0]) if sp[0].isdigit() else 0
                mins = int(sp[1].rstrip("m")) if len(sp) > 1 and sp[1].rstrip("m").isdigit() else 10
                self._post_system_notice(self.reminder_service.snooze(rid, mins))
            elif sub in ("del", "delete", "remove") and arg2.isdigit():
                ok = self.storage.delete_reminder(int(arg2))
                self._post_system_notice(f"Deleted reminder #{arg2}." if ok else f"Reminder #{arg2} not found.")
            elif arg1 and sub not in ("list",):
                full_rem = (arg1 + " " + arg2).strip()
                self._post_system_notice(self.reminder_service.add_from_natural_language(full_rem))
            else:
                rems = self.storage.list_reminders(active_only=False)
                if not rems:
                    self._post_system_notice("No reminders scheduled.")
                else:
                    lines = ["### Reminders"]
                    for r in rems:
                        rec = f" ({r['recurrence']})" if r["recurrence"] else ""
                        lines.append(f"- `#{r['id']}` [{r['status']}] {r['trigger_time']} — {r['message']}{rec}")
                    self._post_markdown_reply("\n".join(lines))

        elif cmd == "/tasks":
            sub = arg1.lower()
            if sub == "add" and arg2:
                tid = self.storage.add_task(arg2)
                self._post_system_notice(f"Added task #{tid}: {arg2}")
            elif sub in ("done", "complete") and arg2.isdigit():
                ok = self.storage.complete_task(int(arg2))
                self._post_system_notice(f"Task #{arg2} completed." if ok else f"Task #{arg2} not found.")
            elif sub in ("del", "delete") and arg2.isdigit():
                ok = self.storage.delete_task(int(arg2))
                self._post_system_notice(f"Task #{arg2} deleted." if ok else f"Task #{arg2} not found.")
            else:
                tasks = self.storage.list_tasks(include_completed=True)
                if not tasks:
                    self._post_system_notice("No tasks found.")
                else:
                    lines = ["### Tasks"]
                    for t in tasks:
                        chk = "[x]" if t["status"] == "done" else "[ ]"
                        lines.append(f"- `{chk}` `#{t['id']}` {t['title']} ({t['priority']})")
                    self._post_markdown_reply("\n".join(lines))

        elif cmd == "/clear":
            self.agent.clear_history()
            chat_scroll = self.query_one("#chat-scroll", VerticalScroll)
            for child in list(chat_scroll.children):
                child.remove()
            self._post_system_notice("Conversation cleared.")

        elif cmd == "/cwd":
            target_dir = (arg1 + " " + arg2).strip()
            if not target_dir:
                self._post_system_notice(f"cwd: {self.cwd}")
            else:
                new_p = Path(target_dir).expanduser()
                if not new_p.is_absolute():
                    new_p = (self.cwd / new_p).resolve()
                if new_p.exists() and new_p.is_dir():
                    self.cwd = new_p
                    self.tool_registry.set_cwd(new_p)
                    self._refresh_chrome()
                    self._post_system_notice(f"cwd changed to {self.cwd}")
                else:
                    self._post_system_notice(f"Directory not found: {new_p}")

        elif cmd == "/auto":
            self.action_toggle_auto_approve()
            self._post_system_notice(f"auto-approve {'on' if self.config_data.auto_approve else 'off'}")

        elif cmd == "/export":
            fname = (arg1 + " " + arg2).strip() or f"cloud_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            out_path = self.cwd / fname
            out_path.write_text("\n\n".join(self._transcript_log), encoding="utf-8")
            self._post_system_notice(f"Exported transcript to {out_path}")

        elif cmd == "/settings":
            self.push_screen(SettingsModal(self.config_data), self._after_modal_notice)

        elif cmd == "/exit":
            self.exit()

        else:
            self._post_system_notice(f"Unknown command {cmd}. Type /help.")

    @work(exclusive=False)
    async def _run_usage_command(self) -> None:
        report = await self.provider_mgr.refresh_live_usage()
        self._refresh_chrome()
        self._post_markdown_reply("### Remaining Usage & Daily Quotas\n```text\n" + report + "\n```")

    def _post_user_message(self, user_input: str) -> None:
        self._switch_to_chat_view()
        chat_scroll = self.query_one("#chat-scroll", VerticalScroll)
        direction = detect_direction(user_input)
        shaped = format_bidirectional_text(user_input, enable_shaping=self.config_data.rtl_shaping)
        cls = "user-msg-row user-msg-rtl" if direction == "rtl" else "user-msg-row"
        chat_scroll.mount(Static(f"[#5fd1a0]❯[/] [#d7dedb]{shaped}[/]", classes=cls))
        chat_scroll.scroll_end(animate=False)

    def _post_markdown_reply(self, md_text: str) -> None:
        self._switch_to_chat_view()
        chat_scroll = self.query_one("#chat-scroll", VerticalScroll)
        now_hm = datetime.now().strftime("%H:%M")
        model_str = self._get_active_model_label()
        shaped = format_bidirectional_text(md_text, enable_shaping=self.config_data.rtl_shaping)
        direction = detect_direction(md_text)
        body_cls = "ai-reply-body ai-reply-rtl" if direction == "rtl" else "ai-reply-body"
        block = Vertical(
            Label(f"[#7f8b87]cloud · [/][#7ab8ff]{model_str}[/][#7f8b87] · {now_hm}[/]", classes="ai-meta-line"),
            Markdown(shaped, classes=body_cls),
            classes="ai-msg-block",
        )
        chat_scroll.mount(block)
        chat_scroll.scroll_end(animate=False)

    # ── Agent Turn Execution & Streaming ──────────────────────────────────────

    def _start_agent_turn(self, user_input: str) -> None:
        if user_input.lower().startswith("remind me ") or user_input.startswith("ذكرني "):
            self._post_user_message(user_input)
            msg = self.reminder_service.add_from_natural_language(user_input)
            self._post_system_notice(msg)
            return

        self._post_user_message(user_input)
        self._transcript_log.append(f"## User\n{user_input}")
        self._active_worker = self._execute_turn_worker(user_input)

    @work(exclusive=True)
    async def _execute_turn_worker(self, user_input: str) -> None:
        chat_scroll = self.query_one("#chat-scroll", VerticalScroll)
        now_hm = datetime.now().strftime("%H:%M")
        model_str = self._get_active_model_label()
        assistant_md = Markdown("", classes="ai-reply-body")
        meta_lbl = Label(
            f"[#7f8b87]cloud · [/][#7ab8ff]{model_str}[/][#7f8b87] · {now_hm}[/]",
            classes="ai-meta-line",
        )
        ai_block = Vertical(meta_lbl, assistant_md, classes="ai-msg-block")
        await chat_scroll.mount(ai_block)
        chat_scroll.scroll_end(animate=False)

        streamed_chunks: List[str] = []
        last_tool_start = time.perf_counter()

        def _on_token(tok: str) -> None:
            streamed_chunks.append(tok)
            full_txt = "".join(streamed_chunks)
            if detect_direction(full_txt) == "rtl":
                assistant_md.add_class("ai-reply-rtl")
            shaped = format_bidirectional_text(full_txt, enable_shaping=self.config_data.rtl_shaping)
            assistant_md.update(shaped)
            chat_scroll.scroll_end(animate=False)

        def _on_tool_result(name: str, args: Dict[str, Any], res: ToolExecutionResult) -> None:
            nonlocal last_tool_start
            elapsed = max(0.1, time.perf_counter() - last_tool_start)
            last_tool_start = time.perf_counter()

            primary_arg = ""
            for k in ("path", "command", "query", "url", "title", "text"):
                if k in args and args[k]:
                    primary_arg = str(args[k])[:42]
                    break
            if not primary_arg and args:
                first_val = next(iter(args.values()))
                primary_arg = str(first_val)[:42]

            state_word = "done" if res.success else "failed"
            summary = f"[#d7dedb]{name}[/] [#7f8b87]{primary_arg}   {state_word} · {elapsed:.1f}s[/]"

            detail_lines = []
            if res.diff_or_preview:
                detail_lines.append(res.diff_or_preview)
            detail_lines.append(res.output)
            item = ToolCallItem(summary, "\n".join(detail_lines))
            chat_scroll.mount(item, before=ai_block)
            chat_scroll.scroll_end(animate=False)

        async def _request_approval(tool_name: str, title: str, preview: str) -> bool:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[bool] = loop.create_future()

            def _on_dismiss(decision: Optional[bool]) -> None:
                if not fut.done():
                    fut.set_result(bool(decision))

            self.push_screen(ApprovalModal(tool_name, title, preview), _on_dismiss)
            return await fut

        try:
            final_text = await self.agent.run_turn(
                user_input=user_input,
                on_token=_on_token,
                on_tool_result=_on_tool_result,
                on_notice=self._post_system_notice,
                approval_callback=_request_approval,
            )
            new_model_str = self._get_active_model_label()
            meta_lbl.update(f"[#7f8b87]cloud · [/][#7ab8ff]{new_model_str}[/][#7f8b87] · {now_hm}[/]")
            if final_text:
                self._transcript_log.append(f"## Cloud\n{final_text}")
            elif not streamed_chunks:
                assistant_md.update("_(done)_")
        except asyncio.CancelledError:
            assistant_md.update("_(cancelled)_")
        except Exception as exc:
            self._post_system_notice(str(exc))
        finally:
            self._refresh_chrome()
            chat_scroll.scroll_end(animate=False)
