"""Interactive Textual ModalScreens for Provider setup, Model Picker (F3), Approvals, Commands (/), Files (@), Integrations, and Settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from cloud_agent.config import AppConfig, PROVIDER_PRESETS, SecretVault, get_windows_clipboard
from cloud_agent.plugins.base import PluginManager, run_oauth_browser_flow
from cloud_agent.providers import ProviderManager


SLASH_COMMANDS_INFO: List[Tuple[str, str]] = [
    ("/help", "Show all commands, shortcuts, and usage guide"),
    ("/usage", "Check live remaining usage & daily request limits"),
    ("/cloudmd", "Show active custom instructions from cloud.md"),
    ("/model", "Open Model Picker (F3) to switch active model"),
    ("/provider add", "Add or update an AI provider & encrypted API key"),
    ("/provider list", "List configured providers and model statuses"),
    ("/provider remove", "Remove a provider and delete its key from DPAPI vault"),
    ("/integrations", "Manage Gmail, Google Calendar, Notion, and MCP plugins"),
    ("/memory", "View learned user preferences, facts, and habits"),
    ("/forget", "Delete a memory by ID, keyword, or 'all'"),
    ("/reminders", "List, add, or snooze smart reminders"),
    ("/tasks", "Manage personal assistant tasks (add/list/complete/delete)"),
    ("/clear", "Clear current chat conversation history"),
    ("/cwd", "View or change current working directory"),
    ("/auto", "Toggle auto-approve mode (Shift+Tab)"),
    ("/export", "Export chat transcript to a Markdown file"),
    ("/settings", "Open Cloud settings dialog"),
    ("/exit", "Exit Cloud AI Agent"),
]


class ProviderAddModal(ModalScreen[Optional[str]]):
    """Interactive dialog to add/update a provider and test its API key on save."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, provider_mgr: ProviderManager, initial_name: str = "openai") -> None:
        super().__init__()
        self.provider_mgr = provider_mgr
        self.initial_name = initial_name.strip().lower() or "openai"

    def compose(self) -> ComposeResult:
        preset = PROVIDER_PRESETS.get(self.initial_name, {})
        default_url = preset.get("base_url", "https://api.openai.com/v1")
        default_models = ", ".join(preset.get("default_models", ["gpt-4o-mini"]))

        with Vertical(classes="modal-card"):
            yield Label("Add / Configure AI Provider", classes="modal-title")
            yield Label(
                "Presets: openai, anthropic, gemini, openrouter, groq, deepseek, mistral, ollama (or custom name)",
                classes="modal-subtitle",
            )
            yield Label("Provider Name:")
            yield Input(value=self.initial_name, placeholder="e.g. openai, groq, openrouter, custom", id="prov-name", classes="modal-input")
            yield Label("Base URL:")
            yield Input(value=default_url, placeholder="https://api.openai.com/v1", id="prov-url", classes="modal-input")
            yield Label("API Key (stored encrypted via Windows DPAPI / Keyring):")
            yield Input(password=True, placeholder="sk-... (leave empty for local Ollama)", id="prov-key", classes="modal-input")
            yield Label("Models (comma-separated):")
            yield Input(value=default_models, placeholder="gpt-4o, gpt-4o-mini", id="prov-models", classes="modal-input")
            yield Label("", id="prov-status", classes="modal-subtitle")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Paste Key", variant="warning", id="btn-paste-key")
                yield Button("Test & Save", variant="success", id="btn-test-save")
                yield Button("Save Without Test", variant="primary", id="btn-save-only")
                yield Button("Cancel", variant="default", id="btn-cancel")

    @on(Button.Pressed, "#btn-paste-key")
    def _on_paste_key(self) -> None:
        clip = get_windows_clipboard().strip()
        if clip:
            key_inp = self.query_one("#prov-key", Input)
            key_inp.value = clip
            key_inp.focus()
            self.query_one("#prov-status", Label).update(f"[#4ade80]Pasted key from clipboard ({len(clip)} chars).[/]")
        else:
            self.query_one("#prov-status", Label).update("[#f87171]Clipboard is empty.[/]")

    @on(Input.Changed, "#prov-name")
    def _on_name_changed(self, event: Input.Changed) -> None:
        key = event.value.strip().lower()
        if key in PROVIDER_PRESETS:
            preset = PROVIDER_PRESETS[key]
            self.query_one("#prov-url", Input).value = preset["base_url"]
            self.query_one("#prov-models", Input).value = ", ".join(preset["default_models"])

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-save-only")
    def _save_without_test(self) -> None:
        name = self.query_one("#prov-name", Input).value.strip()
        url = self.query_one("#prov-url", Input).value.strip()
        key = self.query_one("#prov-key", Input).value.strip()
        models = [m.strip() for m in self.query_one("#prov-models", Input).value.split(",") if m.strip()]
        if not name or not url or not models:
            self.query_one("#prov-status", Label).update("[#f87171]Please provide name, base URL, and at least one model.[/]")
            return
        entry = self.provider_mgr.add_or_update_provider(name, url, key, models)
        self.provider_mgr.set_active_model(entry.name, entry.models[0])
        self.dismiss(f"Saved provider '{entry.display_name}' with {len(entry.models)} model(s).")

    @on(Button.Pressed, "#btn-test-save")
    def _on_test_save(self) -> None:
        self._run_test_and_save()

    @work(exclusive=True)
    async def _run_test_and_save(self) -> None:
        status_lbl = self.query_one("#prov-status", Label)
        name = self.query_one("#prov-name", Input).value.strip()
        url = self.query_one("#prov-url", Input).value.strip()
        key = self.query_one("#prov-key", Input).value.strip()
        models = [m.strip() for m in self.query_one("#prov-models", Input).value.split(",") if m.strip()]

        if not name or not url or not models:
            status_lbl.update("[#f87171]Please fill in Provider Name, Base URL, and Models.[/]")
            return

        status_lbl.update("[#38bdf8]Testing connection & API key...[/]")
        fmt = "anthropic" if "anthropic" in name.lower() or "anthropic.com" in url.lower() else "openai"
        ok, msg, quota_bar = await self.provider_mgr.test_provider_connection(name, url, key, models[0], fmt)
        entry = self.provider_mgr.add_or_update_provider(name, url, key, models, api_format=fmt)
        self.provider_mgr.mark_model_status(
            entry.name,
            models[0],
            "available" if ok else "unavailable",
            quota_bar,
        )
        self.provider_mgr.set_active_model(entry.name, models[0])
        if ok:
            self.dismiss(f"Provider '{entry.display_name}' verified & saved: {msg}")
        else:
            self.dismiss(f"Provider '{entry.display_name}' saved (Test status: {msg})")


class ModelPickerModal(ModalScreen[Optional[str]]):
    """F3 / /model picker displaying status, quota bar, and daily requests for each model."""

    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, provider_mgr: ProviderManager) -> None:
        super().__init__()
        self.provider_mgr = provider_mgr
        self._items: List[Dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        self._items = self.provider_mgr.get_all_models()
        with Vertical(classes="modal-card"):
            yield Label("Select Active Model & Remaining Usage (F3)", classes="modal-title")
            yield Label(
                "Status: [#5fd1a0]available[/] · [#e5b567]quota_exceeded[/] · [#f87171]unavailable[/]",
                classes="modal-subtitle",
                id="model-picker-sub",
            )
            opt_list = OptionList(id="model-opt-list")
            self._populate_model_options(opt_list)
            yield opt_list
            with Horizontal(classes="modal-btn-row"):
                yield Button("Refresh Usage", variant="default", id="btn-refresh-usage")
                yield Button("Add Provider (+)", variant="default", id="btn-add-prov")
                yield Button("Close (Esc)", variant="default", id="btn-close")

    def on_mount(self) -> None:
        self._refresh_usage_worker()

    def _populate_model_options(self, opt_list: OptionList) -> None:
        opt_list.clear_options()
        self._items = self.provider_mgr.get_all_models()
        if not self._items:
            opt_list.add_option(Option("No models configured yet — click 'Add Provider (+)' below", id="__empty__"))
            return
        for idx, item in enumerate(self._items):
            marker = "*" if item["is_active"] else " "
            st = item["status"].upper()
            line = (
                f"[{marker}] {item['provider']}/{item['model']}  |  "
                f"{st}  |  {item['quota']}  |  used today: {item['requests_today']}"
            )
            opt_list.add_option(Option(line, id=str(idx)))

    @on(Button.Pressed, "#btn-refresh-usage")
    def _on_refresh_btn(self) -> None:
        self._refresh_usage_worker()

    @work(exclusive=True)
    async def _refresh_usage_worker(self) -> None:
        sub = self.query_one("#model-picker-sub", Label)
        sub.update("[#7ab8ff]Checking live remaining usage & daily limits...[/]")
        await self.provider_mgr.refresh_live_usage()
        opt_list = self.query_one("#model-opt-list", OptionList)
        self._populate_model_options(opt_list)
        sub.update("Status: [#5fd1a0]available[/] · [#e5b567]quota_exceeded[/] · [#f87171]unavailable[/]  (Usage updated live)")

    @on(OptionList.OptionSelected, "#model-opt-list")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        opt_id = str(event.option_id)
        if opt_id == "__empty__":
            self.dismiss("__add_provider__")
            return
        idx = int(opt_id)
        chosen = self._items[idx]
        self.provider_mgr.set_active_model(chosen["provider"], chosen["model"])
        self.dismiss(f"Active model set to {chosen['provider']}/{chosen['model']}")

    @on(Button.Pressed, "#btn-add-prov")
    def _on_add(self) -> None:
        self.dismiss("__add_provider__")

    @on(Button.Pressed, "#btn-close")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ApprovalModal(ModalScreen[bool]):
    """Diff/Command preview dialog asking user approval (Y/N) before modifying files or running shell."""

    BINDINGS = [
        Binding("y", "approve", "Approve (Y)"),
        Binding("n", "reject", "Reject (N)"),
        Binding("escape", "reject", "Reject"),
    ]

    def __init__(self, tool_name: str, title: str, preview: str) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.title_text = title
        self.preview = preview

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-card"):
            yield Label(f"Approval Required: {self.tool_name}", classes="modal-title")
            yield Label(self.title_text, classes="modal-subtitle")
            with VerticalScroll(id="diff-preview-box"):
                yield Static(self.preview, markup=False)
            with Horizontal(classes="modal-btn-row"):
                yield Button("Approve (Y)", variant="success", id="btn-approve")
                yield Button("Reject (N)", variant="error", id="btn-reject")

    @on(Button.Pressed, "#btn-approve")
    def action_approve(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-reject")
    def action_reject(self) -> None:
        self.dismiss(False)


class CommandPaletteModal(ModalScreen[Optional[str]]):
    """Slash command palette opened with '/'."""

    BINDINGS = [Binding("escape", "cancel", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-card"):
            yield Label("Slash Commands (/)", classes="modal-title")
            yield Input(placeholder="Filter commands...", id="cmd-filter", classes="modal-input")
            opt_list = OptionList(id="cmd-opt-list")
            for cmd, desc in SLASH_COMMANDS_INFO:
                opt_list.add_option(Option(f"{cmd:<18} — {desc}", id=cmd))
            yield opt_list
            with Horizontal(classes="modal-btn-row"):
                yield Button("Close", variant="default", id="btn-close-cmd")

    @on(Input.Changed, "#cmd-filter")
    def _on_filter(self, event: Input.Changed) -> None:
        q = event.value.strip().lower()
        opt_list = self.query_one("#cmd-opt-list", OptionList)
        opt_list.clear_options()
        for cmd, desc in SLASH_COMMANDS_INFO:
            if q in cmd.lower() or q in desc.lower():
                opt_list.add_option(Option(f"{cmd:<18} — {desc}", id=cmd))

    @on(OptionList.OptionSelected, "#cmd-opt-list")
    def _on_pick(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option_id))

    @on(Button.Pressed, "#btn-close-cmd")
    def action_cancel(self) -> None:
        self.dismiss(None)


class FileAutocompleteModal(ModalScreen[Optional[str]]):
    """File picker opened with '@' to attach project files."""

    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, cwd: Path) -> None:
        super().__init__()
        self.cwd = cwd
        self._files: List[str] = self._scan_files()

    def _scan_files(self) -> List[str]:
        out: List[str] = []
        skip = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
        for root, dirs, files in os.walk(self.cwd):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in files:
                full = Path(root) / f
                try:
                    rel = full.relative_to(self.cwd).as_posix()
                    out.append(rel)
                except ValueError:
                    pass
                if len(out) >= 200:
                    break
            if len(out) >= 200:
                break
        return sorted(out)

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-card"):
            yield Label(f"Attach Project File (@) — {self.cwd}", classes="modal-title")
            yield Input(placeholder="Type filename to filter...", id="file-filter", classes="modal-input")
            opt_list = OptionList(id="file-opt-list")
            for rel in self._files[:100]:
                opt_list.add_option(Option(f"@{rel}", id=rel))
            yield opt_list
            with Horizontal(classes="modal-btn-row"):
                yield Button("Cancel", variant="default", id="btn-close-file")

    @on(Input.Changed, "#file-filter")
    def _on_filter(self, event: Input.Changed) -> None:
        q = event.value.strip().lower()
        opt_list = self.query_one("#file-opt-list", OptionList)
        opt_list.clear_options()
        for rel in self._files:
            if q in rel.lower():
                opt_list.add_option(Option(f"@{rel}", id=rel))

    @on(OptionList.OptionSelected, "#file-opt-list")
    def _on_pick(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(f"@{event.option_id} ")

    @on(Button.Pressed, "#btn-close-file")
    def action_cancel(self) -> None:
        self.dismiss(None)


class IntegrationsModal(ModalScreen[Optional[str]]):
    """Manage optional integrations (Gmail, Google Calendar, Notion) and OAuth / tokens."""

    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, config: AppConfig, plugin_mgr: PluginManager) -> None:
        super().__init__()
        self.config = config
        self.plugin_mgr = plugin_mgr

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-card"):
            yield Label("Integrations & Plugins (/integrations)", classes="modal-title")
            yield Label("Select a plugin to toggle Enable/Disable, or configure OAuth / API Token below.", classes="modal-subtitle")
            opt_list = OptionList(id="int-opt-list")
            self._populate_options(opt_list)
            yield opt_list
            yield Label("Plugin ID (gmail / gcal / notion):")
            yield Input(value="gmail", id="int-id-input", classes="modal-input")
            yield Label("Direct Token OR OAuth Client ID (stored encrypted via DPAPI):")
            yield Input(password=True, placeholder="Paste token or Google/Notion OAuth Client ID", id="int-token-input", classes="modal-input")
            yield Label("OAuth Client Secret (only if launching Browser OAuth flow):")
            yield Input(password=True, placeholder="Optional OAuth Client Secret", id="int-secret-input", classes="modal-input")
            yield Label("", id="int-status-lbl", classes="modal-subtitle")
            with Horizontal(classes="modal-btn-row"):
                yield Button("Save Token", variant="success", id="btn-save-token")
                yield Button("Browser OAuth Flow", variant="primary", id="btn-oauth-browser")
                yield Button("Done", variant="default", id="btn-done-int")

    def _populate_options(self, opt_list: OptionList) -> None:
        opt_list.clear_options()
        for pid, plugin in self.plugin_mgr.plugins.items():
            enabled = pid in self.config.enabled_integrations
            authed = plugin.is_authenticated()
            en_str = "ENABLED" if enabled else "DISABLED"
            au_str = "AUTH OK" if authed else "NO TOKEN"
            opt_list.add_option(
                Option(f"[{en_str}] {plugin.display_name} ({pid}) — {au_str} — {plugin.description}", id=pid)
            )

    @on(OptionList.OptionSelected, "#int-opt-list")
    def _on_toggle_plugin(self, event: OptionList.OptionSelected) -> None:
        pid = str(event.option_id)
        new_state = self.plugin_mgr.toggle_plugin(pid)
        self.query_one("#int-id-input", Input).value = pid
        opt_list = self.query_one("#int-opt-list", OptionList)
        self._populate_options(opt_list)
        self.query_one("#int-status-lbl", Label).update(
            f"[#4ade80]Plugin '{pid}' is now {'ENABLED' if new_state else 'DISABLED'}.[/]"
        )

    @on(Button.Pressed, "#btn-save-token")
    def _on_save_token(self) -> None:
        pid = self.query_one("#int-id-input", Input).value.strip().lower()
        tok = self.query_one("#int-token-input", Input).value.strip()
        if pid not in self.plugin_mgr.plugins or not tok:
            self.query_one("#int-status-lbl", Label).update("[#f87171]Valid plugin ID and token required.[/]")
            return
        self.plugin_mgr.plugins[pid].set_token(tok)
        self.plugin_mgr.toggle_plugin(pid, enable=True)
        self._populate_options(self.query_one("#int-opt-list", OptionList))
        self.query_one("#int-status-lbl", Label).update(f"[#4ade80]Encrypted token saved & '{pid}' enabled.[/]")

    @on(Button.Pressed, "#btn-oauth-browser")
    def _on_oauth(self) -> None:
        self._launch_oauth()

    @work(exclusive=True)
    async def _launch_oauth(self) -> None:
        pid = self.query_one("#int-id-input", Input).value.strip().lower()
        client_id = self.query_one("#int-token-input", Input).value.strip()
        client_secret = self.query_one("#int-secret-input", Input).value.strip()
        lbl = self.query_one("#int-status-lbl", Label)
        if not client_id or not client_secret:
            lbl.update("[#f87171]Enter OAuth Client ID and Client Secret first, then click Browser OAuth Flow.[/]")
            return

        if pid in ("gmail", "gcal"):
            auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
            token_url = "https://oauth2.googleapis.com/token"
            scopes = (
                ["https://www.googleapis.com/auth/gmail.modify"]
                if pid == "gmail"
                else ["https://www.googleapis.com/auth/calendar"]
            )
        else:
            auth_url = "https://api.notion.com/v1/oauth/authorize"
            token_url = "https://api.notion.com/v1/oauth/token"
            scopes = []

        lbl.update("[#38bdf8]Opening browser for OAuth2 consent (waiting on 127.0.0.1:8765)...[/]")
        ok, msg = await run_oauth_browser_flow(pid, auth_url, token_url, client_id, client_secret, scopes)
        if ok:
            self.plugin_mgr.toggle_plugin(pid, enable=True)
            self._populate_options(self.query_one("#int-opt-list", OptionList))
            lbl.update(f"[#4ade80]{msg}[/]")
        else:
            lbl.update(f"[#f87171]{msg}[/]")

    @on(Button.Pressed, "#btn-done-int")
    def action_cancel(self) -> None:
        self.dismiss("Integrations updated.")


class SettingsModal(ModalScreen[Optional[str]]):
    """Settings modal for toggling Cloud preferences."""

    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-card"):
            yield Label("Cloud Settings (/settings)", classes="modal-title")
            opt_list = OptionList(id="settings-opt-list")
            self._refresh_list(opt_list)
            yield opt_list
            with Horizontal(classes="modal-btn-row"):
                yield Button("Save & Close", variant="success", id="btn-close-settings")

    def _refresh_list(self, opt_list: OptionList) -> None:
        opt_list.clear_options()
        items = [
            ("auto_approve", f"Auto-Approve Tool Executions (Shift+Tab): {'ON' if self.config.auto_approve else 'OFF'}"),
            ("daily_briefing", f"Startup Daily Briefing: {'ON' if self.config.daily_briefing else 'OFF'}"),
            ("auto_learn", f"Self-Learning Memory Extraction: {'ON' if self.config.auto_learn else 'OFF'}"),
            ("proactive_suggestions", f"Proactive Workflow Suggestions: {'ON' if self.config.proactive_suggestions else 'OFF'}"),
            ("rtl_shaping", f"Arabic RTL Bidirectional Support: {'ON' if self.config.rtl_shaping else 'OFF'}"),
        ]
        for k, label in items:
            opt_list.add_option(Option(label, id=k))

    @on(OptionList.OptionSelected, "#settings-opt-list")
    def _on_toggle(self, event: OptionList.OptionSelected) -> None:
        k = str(event.option_id)
        val = getattr(self.config, k, False)
        setattr(self.config, k, not val)
        self.config.save()
        self._refresh_list(self.query_one("#settings-opt-list", OptionList))

    @on(Button.Pressed, "#btn-close-settings")
    def action_cancel(self) -> None:
        self.dismiss("Settings saved.")
