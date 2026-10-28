"""Optional native PySide6 GUI (--gui) with the exact same minimal dark layout, colors, and shortcuts, supporting Arabic RTL natively."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from cloud_agent.agent import CloudAgent
from cloud_agent.config import AppConfig
from cloud_agent.memory import MemoryEngine
from cloud_agent.plugins.base import PluginManager
from cloud_agent.providers import ProviderManager
from cloud_agent.reminders import ReminderService
from cloud_agent.rtl import detect_direction
from cloud_agent.storage import CloudStorage
from cloud_agent.tools import ToolExecutionResult, ToolRegistry
from cloud_agent.tui.app import EN_TO_AR_KEYMAP
from cloud_agent.tui.modals import SLASH_COMMANDS_INFO


DARK_STYLESHEET = """
QMainWindow, QWidget#rootWidget, QScrollArea, QWidget#scrollContents {
    background-color: #0b0f0e;
    color: #d7dedb;
    font-family: 'Segoe UI', 'Consolas', 'Courier New', monospace;
    font-size: 14px;
}
QFrame#thinDivider {
    background-color: #1b2421;
    max-height: 1px;
    min-height: 1px;
    border: none;
}
QPlainTextEdit#promptInput {
    background-color: #0b0f0e;
    color: #d7dedb;
    border: none;
    font-family: 'Segoe UI', 'Consolas', monospace;
    font-size: 14px;
    selection-background-color: #1b2421;
}
QDialog {
    background-color: #0b0f0e;
    color: #d7dedb;
    border: 1px solid #1b2421;
}
QListWidget, QLineEdit, QPlainTextEdit#diffBox {
    background-color: #111715;
    color: #d7dedb;
    border: 1px solid #1b2421;
    padding: 4px;
}
QPushButton {
    background-color: #121917;
    color: #d7dedb;
    border: 1px solid #1b2421;
    padding: 5px 14px;
}
QPushButton:hover {
    border: 1px solid #5fd1a0;
    color: #5fd1a0;
}
QScrollBar:vertical {
    background: #0b0f0e;
    width: 6px;
}
QScrollBar::handle:vertical {
    background: #1b2421;
}
"""


class _UiBridge(QObject):
    token_received = Signal(str)
    tool_received = Signal(str, str)
    notice_received = Signal(str)
    turn_finished = Signal(str)
    approval_requested = Signal(str, str, str)


class _ToolExpandWidget(QWidget):
    """Single collapsed tool line ('▸ read_file report.csv   done · 0.2s'), expandable on Enter/click."""

    def __init__(self, summary: str, details: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.summary = summary
        self.details = details
        self._expanded = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 2, 0, 2)
        layout.setSpacing(2)

        self.header_btn = QPushButton(f"▸ {summary}")
        self.header_btn.setFlat(True)
        self.header_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header_btn.setStyleSheet(
            "QPushButton { text-align: left; background: transparent; border: none; color: #7f8b87; padding: 2px 0; }"
            "QPushButton:focus, QPushButton:hover { color: #d7dedb; }"
        )
        self.header_btn.clicked.connect(self.toggle)

        self.details_lbl = QLabel(details)
        self.details_lbl.setWordWrap(True)
        self.details_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details_lbl.setStyleSheet(
            "color: #7f8b87; background-color: #0e1412; border-left: 1px solid #1b2421; padding: 6px 10px; font-family: 'Consolas', monospace; font-size: 12px;"
        )
        self.details_lbl.setVisible(False)

        layout.addWidget(self.header_btn)
        layout.addWidget(self.details_lbl)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        icon = "▾" if self._expanded else "▸"
        self.header_btn.setText(f"{icon} {self.summary}")
        self.details_lbl.setVisible(self._expanded)


class _GrowingInput(QPlainTextEdit):
    """Single-line input that grows up to 6 lines, submits on Enter, and supports F2 Arabic keymap."""

    submitted = Signal(str)
    slash_pressed = Signal()
    at_pressed = Signal()

    def __init__(self, main_win: "CloudGuiWindow") -> None:
        super().__init__()
        self.main_win = main_win
        self.setObjectName("promptInput")
        self.setPlaceholderText("Ask for a change, or / for commands")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(26)
        self.textChanged.connect(self._adjust_height)

    def _adjust_height(self) -> None:
        doc_lines = max(1, min(6, self.document().blockCount()))
        self.setFixedHeight(doc_lines * 22 + 4)
        txt = self.toPlainText()
        if detect_direction(txt) == "rtl":
            self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        else:
            self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

    def keyPressEvent(self, event: Any) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            txt = self.toPlainText().strip()
            self.clear()
            self.setFixedHeight(26)
            if txt:
                self.submitted.emit(txt)
            return

        ch = event.text()
        if ch == "/" and not self.toPlainText():
            self.slash_pressed.emit()
            return
        if ch == "@" and (not self.toPlainText() or self.toPlainText().endswith(" ")):
            self.at_pressed.emit()
            return

        if self.main_win.arabic_kbd_mode and ch and len(ch) == 1 and ch in EN_TO_AR_KEYMAP:
            self.insertPlainText(EN_TO_AR_KEYMAP[ch])
            return

        super().keyPressEvent(event)


class CloudGuiWindow(QMainWindow):
    """Native Windows GUI window matching the exact TUI design and supporting native Arabic RTL."""

    def __init__(self, initial_cwd: Optional[Path] = None) -> None:
        super().__init__()
        self.cwd = (initial_cwd or Path.cwd()).resolve()
        self.config_data = AppConfig.load()
        self.storage = CloudStorage()
        self.provider_mgr = ProviderManager(self.config_data, self.storage)
        self.reminder_service = ReminderService(
            self.storage,
            ui_notify_callback=lambda msg: self.bridge.notice_received.emit(msg),
        )
        self.tool_registry = ToolRegistry(
            self.storage,
            self.cwd,
            reminder_callback=lambda act, txt: self.reminder_service.add_from_natural_language(txt) if act == "add" else "",
        )
        self.plugin_mgr = PluginManager(self.config_data, self.tool_registry)
        self.plugin_mgr.sync_enabled_tools()
        self.memory_engine = MemoryEngine(self.storage)
        self.agent = CloudAgent(
            self.config_data,
            self.storage,
            self.provider_mgr,
            self.tool_registry,
            self.memory_engine,
        )

        self.arabic_kbd_mode = False
        self._cancel_flag = False
        self._approval_event = threading.Event()
        self._approval_decision = False
        self._current_ai_label: Optional[QLabel] = None
        self._current_ai_text: List[str] = []

        self.bridge = _UiBridge()
        self.bridge.token_received.connect(self._on_token_ui)
        self.bridge.tool_received.connect(self._on_tool_ui)
        self.bridge.notice_received.connect(self._post_notice)
        self.bridge.turn_finished.connect(self._on_turn_done_ui)
        self.bridge.approval_requested.connect(self._on_approval_ui)

        self._build_ui()
        self._setup_shortcuts()
        self.reminder_service.start()
        self._refresh_chrome()

    def _build_ui(self) -> None:
        self.setWindowTitle("Cloud")
        self.resize(1020, 680)
        self.setStyleSheet(DARK_STYLESHEET)
        for icon_name in ("cloud.png", "cloud.ico"):
            for base_dir in (self.cwd, Path(sys.executable).resolve().parent, Path(__file__).resolve().parent.parent.parent):
                candidate = base_dir / icon_name
                if candidate.is_file():
                    self.setWindowIcon(QIcon(str(candidate)))
                    break

        root = QWidget()
        root.setObjectName("rootWidget")
        self.setCentralWidget(root)
        main_vbox = QVBoxLayout(root)
        main_vbox.setContentsMargins(0, 0, 0, 0)
        main_vbox.setSpacing(0)

        # 1. Slim Header (1 line + thin bottom divider)
        hdr_widget = QWidget()
        hdr_layout = QHBoxLayout(hdr_widget)
        hdr_layout.setContentsMargins(18, 6, 18, 6)
        self.hdr_left = QLabel()
        self.hdr_right = QLabel()
        hdr_layout.addWidget(self.hdr_left)
        hdr_layout.addStretch(1)
        hdr_layout.addWidget(self.hdr_right)
        main_vbox.addWidget(hdr_widget)

        div_top = QFrame()
        div_top.setObjectName("thinDivider")
        main_vbox.addWidget(div_top)

        # 2. Message Scroll Area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_contents = QWidget()
        self.scroll_contents.setObjectName("scrollContents")
        self.msg_vbox = QVBoxLayout(self.scroll_contents)
        self.msg_vbox.setContentsMargins(24, 18, 24, 18)
        self.msg_vbox.setSpacing(14)
        self.msg_vbox.addStretch(1)
        self.scroll_area.setWidget(self.scroll_contents)
        main_vbox.addWidget(self.scroll_area, 1)

        # 3. Thin divider above input + Input row
        div_bot = QFrame()
        div_bot.setObjectName("thinDivider")
        main_vbox.addWidget(div_bot)

        inp_container = QWidget()
        inp_hbox = QHBoxLayout(inp_container)
        inp_hbox.setContentsMargins(18, 6, 18, 4)
        inp_hbox.setSpacing(6)
        prompt_lbl = QLabel("<span style='color:#5fd1a0; font-weight:bold;'>❯</span>")
        self.input_box = _GrowingInput(self)
        self.input_box.submitted.connect(self._on_submit)
        self.input_box.slash_pressed.connect(self._open_command_palette)
        self.input_box.at_pressed.connect(self._open_file_picker)
        inp_hbox.addWidget(prompt_lbl, 0, Qt.AlignmentFlag.AlignTop)
        inp_hbox.addWidget(self.input_box, 1)
        main_vbox.addWidget(inp_container)

        # 4. Slim 1-Line Footer
        ftr_widget = QWidget()
        ftr_hbox = QHBoxLayout(ftr_widget)
        ftr_hbox.setContentsMargins(18, 2, 18, 6)
        self.ftr_left = QLabel()
        self.ftr_right = QLabel()
        ftr_hbox.addWidget(self.ftr_left)
        ftr_hbox.addStretch(1)
        ftr_hbox.addWidget(self.ftr_right)
        main_vbox.addWidget(ftr_widget)

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("F2"), self, activated=self._toggle_arabic_kbd)
        QShortcut(QKeySequence("F3"), self, activated=self._open_model_picker)
        QShortcut(QKeySequence("Shift+Tab"), self, activated=self._toggle_auto_approve)
        QShortcut(QKeySequence("Ctrl+C"), self, activated=self._cancel_task)

    def _get_model_label(self) -> str:
        if self.config_data.active_provider and self.config_data.active_model:
            return f"{self.config_data.active_provider}/{self.config_data.active_model}"
        return "no-model"

    def _refresh_chrome(self) -> None:
        m_str = self._get_model_label()
        self.hdr_left.setText(
            "<span style='color:#5fd1a0; font-weight:bold;'>C L O U D</span>"
            "  <span style='color:#7f8b87;'>v1.0</span>"
        )
        self.hdr_right.setText(
            f"<span style='color:#7ab8ff;'>{m_str}</span>"
            "  <span style='color:#7f8b87;'>F3 models</span>"
        )

        cwd_str = str(self.cwd)
        if len(cwd_str) > 38:
            cwd_str = "..." + cwd_str[-35:]
        ar_tag = "<span style='color:#5fd1a0;'>F2 Arabic:ON</span>" if self.arabic_kbd_mode else "F2 Arabic"
        auto_tag = (
            "<span style='color:#5fd1a0;'>auto-approve on</span>"
            if self.config_data.auto_approve
            else "auto-approve off"
        )
        self.ftr_left.setText(f"<span style='color:#7f8b87;'>{m_str} · {cwd_str}</span>")
        self.ftr_right.setText(
            f"<span style='color:#7f8b87;'>/ commands · @ files · {ar_tag} · Agent · {auto_tag}</span>"
        )

    def _scroll_to_bottom(self) -> None:
        QApplication.processEvents()
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _add_message_widget(self, w: QWidget) -> None:
        idx = max(0, self.msg_vbox.count() - 1)
        self.msg_vbox.insertWidget(idx, w)
        self._scroll_to_bottom()

    def _post_user_msg(self, text: str) -> None:
        lbl = QLabel(f"<span style='color:#5fd1a0; font-weight:bold;'>❯</span> <span style='color:#d7dedb;'>{text}</span>")
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if detect_direction(text) == "rtl":
            lbl.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._add_message_widget(lbl)

    def _post_notice(self, text: str) -> None:
        clean = text.lstrip("•↻ ").strip()
        lbl = QLabel(f"<span style='color:#e5b567;'>↻ {clean}</span>")
        lbl.setWordWrap(True)
        if detect_direction(clean) == "rtl":
            lbl.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._add_message_widget(lbl)
        self._refresh_chrome()

    def _toggle_arabic_kbd(self) -> None:
        self.arabic_kbd_mode = not self.arabic_kbd_mode
        self._refresh_chrome()

    def _toggle_auto_approve(self) -> None:
        self.config_data.auto_approve = not self.config_data.auto_approve
        self.config_data.save()
        self._refresh_chrome()

    def _cancel_task(self) -> None:
        self._cancel_flag = True
        self._post_notice("Task cancelled.")

    def _open_model_picker(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Select Model (F3)")
        dlg.resize(580, 340)
        lay = QVBoxLayout(dlg)
        lw = QListWidget()
        models = self.provider_mgr.get_all_models()
        for idx, m in enumerate(models):
            act = "●" if m["is_active"] else "○"
            item = QListWidgetItem(
                f"{act} {m['provider']}/{m['model']}  ·  {m['status']}  ·  {m['quota']}  ·  today: {m['requests_today']}"
            )
            item.setData(Qt.ItemDataRole.UserRole, idx)
            lw.addItem(item)
        lay.addWidget(lw)

        def _choose() -> None:
            cur = lw.currentItem()
            if cur:
                chosen = models[int(cur.data(Qt.ItemDataRole.UserRole))]
                self.provider_mgr.set_active_model(chosen["provider"], chosen["model"])
                self._post_notice(f"Active model set to {chosen['provider']}/{chosen['model']}")
                dlg.accept()

        lw.itemDoubleClicked.connect(lambda _: _choose())
        btn_ok = QPushButton("Select")
        btn_ok.clicked.connect(_choose)
        lay.addWidget(btn_ok)
        dlg.exec()

    def _open_command_palette(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Commands (/)")
        dlg.resize(520, 320)
        lay = QVBoxLayout(dlg)
        lw = QListWidget()
        for cmd, desc in SLASH_COMMANDS_INFO:
            it = QListWidgetItem(f"{cmd:<18} — {desc}")
            it.setData(Qt.ItemDataRole.UserRole, cmd)
            lw.addItem(it)
        lay.addWidget(lw)

        def _pick() -> None:
            cur = lw.currentItem()
            if cur:
                cmd = str(cur.data(Qt.ItemDataRole.UserRole))
                dlg.accept()
                self._on_submit(cmd)

        lw.itemDoubleClicked.connect(lambda _: _pick())
        dlg.exec()

    def _open_file_picker(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Attach File (@)")
        dlg.resize(520, 320)
        lay = QVBoxLayout(dlg)
        lw = QListWidget()
        for f in sorted(self.cwd.glob("*")):
            if f.is_file():
                lw.addItem(f.name)
        lay.addWidget(lw)

        def _pick() -> None:
            cur = lw.currentItem()
            if cur:
                self.input_box.insertPlainText(f"@{cur.text()} ")
                dlg.accept()

        lw.itemDoubleClicked.connect(lambda _: _pick())
        dlg.exec()

    def _on_submit(self, raw_text: str) -> None:
        if raw_text.startswith("/"):
            if raw_text == "/model":
                self._open_model_picker()
            elif raw_text == "/auto":
                self._toggle_auto_approve()
            elif raw_text == "/clear":
                while self.msg_vbox.count() > 1:
                    item = self.msg_vbox.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()
                self.agent.clear_history()
            elif raw_text == "/exit":
                self.close()
            else:
                self._post_notice(f"Command executed: {raw_text}")
            return

        self._post_user_msg(raw_text)

        # Prepare AI message block: dim meta line + indented reply body
        now_hm = datetime.now().strftime("%H:%M")
        m_str = self._get_model_label()
        block = QWidget()
        bv = QVBoxLayout(block)
        bv.setContentsMargins(0, 4, 0, 4)
        bv.setSpacing(2)
        meta = QLabel(
            f"<span style='color:#7f8b87;'>cloud · </span>"
            f"<span style='color:#7ab8ff;'>{m_str}</span>"
            f"<span style='color:#7f8b87;'> · {now_hm}</span>"
        )
        reply_lbl = QLabel("")
        reply_lbl.setWordWrap(True)
        reply_lbl.setTextFormat(Qt.TextFormat.MarkdownText)
        reply_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        reply_lbl.setStyleSheet("color: #d7dedb; padding-left: 16px;")
        bv.addWidget(meta)
        bv.addWidget(reply_lbl)
        self._add_message_widget(block)

        self._current_ai_label = reply_lbl
        self._current_ai_text = []
        self._cancel_flag = False

        threading.Thread(target=self._run_agent_thread, args=(raw_text,), daemon=True).start()

    def _run_agent_thread(self, user_input: str) -> None:
        t0 = time.perf_counter()

        def _on_tok(tok: str) -> None:
            if not self._cancel_flag:
                self.bridge.token_received.emit(tok)

        def _on_tool(name: str, args: Dict[str, Any], res: ToolExecutionResult) -> None:
            nonlocal t0
            elapsed = max(0.1, time.perf_counter() - t0)
            t0 = time.perf_counter()
            arg_str = str(next(iter(args.values()), ""))[:40] if args else ""
            summary = f"{name} {arg_str}   {'done' if res.success else 'failed'} · {elapsed:.1f}s"
            details = (res.diff_or_preview + "\n" if res.diff_or_preview else "") + res.output
            self.bridge.tool_received.emit(summary, details)

        async def _approval_cb(tool_name: str, title: str, preview: str) -> bool:
            self._approval_event.clear()
            self.bridge.approval_requested.emit(tool_name, title, preview)
            while not self._approval_event.is_set():
                await asyncio.sleep(0.05)
            return self._approval_decision

        async def _exec() -> None:
            try:
                await self.agent.run_turn(
                    user_input=user_input,
                    on_token=_on_tok,
                    on_tool_result=_on_tool,
                    on_notice=lambda m: self.bridge.notice_received.emit(m),
                    approval_callback=_approval_cb,
                )
            except Exception as exc:
                self.bridge.notice_received.emit(str(exc))
            finally:
                self.bridge.turn_finished.emit("")

        asyncio.run(_exec())

    def _on_token_ui(self, tok: str) -> None:
        if self._current_ai_label is None:
            return
        self._current_ai_text.append(tok)
        full = "".join(self._current_ai_text)
        if detect_direction(full) == "rtl":
            self._current_ai_label.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            self._current_ai_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self._current_ai_label.setText(full)
        self._scroll_to_bottom()

    def _on_tool_ui(self, summary: str, details: str) -> None:
        w = _ToolExpandWidget(summary, details)
        self._add_message_widget(w)

    def _on_turn_done_ui(self, _unused: str) -> None:
        self._refresh_chrome()
        self._scroll_to_bottom()

    def _on_approval_ui(self, tool_name: str, title: str, preview: str) -> None:
        ans = QMessageBox.question(
            self,
            f"Approval: {tool_name}",
            f"{title}\n\n{preview[:1200]}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        self._approval_decision = ans == QMessageBox.StandardButton.Yes
        self._approval_event.set()


def run_gui_app(initial_cwd: Optional[Path] = None) -> int:
    app = QApplication(sys.argv)
    win = CloudGuiWindow(initial_cwd=initial_cwd)
    win.show()
    return app.exec()
