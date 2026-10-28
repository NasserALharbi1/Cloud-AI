"""Agent tools: filesystem, PowerShell, web search/fetch, tasks, notes, CSV/Excel analysis."""

from __future__ import annotations

import asyncio
import csv
import difflib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from cloud_agent.storage import CloudStorage

ApprovalCallback = Callable[[str, str, str], Awaitable[bool]]


@dataclass
class ToolExecutionResult:
    tool_name: str
    success: bool
    output: str
    diff_or_preview: str = ""
    approved: bool = True


def build_unified_diff(old_text: str, new_text: str, filepath: str) -> str:
    """Create a unified diff string between old_text and new_text."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath}",
        lineterm="\n",
    )
    return "".join(diff) or "(No textual changes)"


def resolve_file_mentions(prompt: str, cwd: Path) -> Tuple[str, List[str]]:
    """Find @filepath tokens in user prompt and return (augmented_prompt, attached_files)."""
    pattern = re.compile(r"(?:^|\s)@([^\s,;]+)")
    matches = pattern.findall(prompt)
    if not matches:
        return prompt, []

    attached: List[str] = []
    blocks: List[str] = []
    for rel in matches:
        candidate = (cwd / rel).resolve()
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
                if len(content) > 24000:
                    content = content[:24000] + "\n... [truncated]"
                blocks.append(f"--- Attached File: {rel} ---\n{content}\n--- End of {rel} ---")
                attached.append(rel)
            except Exception as exc:
                blocks.append(f"--- Attached File {rel} could not be read: {exc} ---")
    if not blocks:
        return prompt, []
    augmented = prompt + "\n\n" + "\n\n".join(blocks)
    return augmented, attached


class ToolRegistry:
    """Registry and executor for all built-in and plugin agent tools."""

    def __init__(
        self,
        storage: CloudStorage,
        cwd: Path,
        reminder_callback: Optional[Callable[[str, str], str]] = None,
    ) -> None:
        self.storage = storage
        self.cwd = cwd
        self.reminder_callback = reminder_callback
        self._extra_tools: Dict[str, Tuple[Dict[str, Any], Callable[..., Awaitable[str]]]] = {}

    def set_cwd(self, new_cwd: Path) -> None:
        self.cwd = new_cwd.resolve()

    def register_external_tool(
        self,
        schema: Dict[str, Any],
        handler: Callable[..., Awaitable[str]],
    ) -> None:
        fn_name = schema.get("function", {}).get("name", "")
        if fn_name:
            self._extra_tools[fn_name] = (schema, handler)

    def clear_external_tools(self) -> None:
        self._extra_tools.clear()

    def _resolve_path(self, path_str: str) -> Path:
        p = Path(path_str)
        if not p.is_absolute():
            p = (self.cwd / p).resolve()
        return p

    def get_openai_tools_schema(self) -> List[Dict[str, Any]]:
        """Return OpenAI function-calling schema for all registered tools."""
        schemas: List[Dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read the contents of a file from disk.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative or absolute file path."},
                            "start_line": {"type": "integer", "description": "Optional 1-based start line."},
                            "end_line": {"type": "integer", "description": "Optional 1-based end line."},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Create or overwrite a file with new content (shows diff and asks approval).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path to write."},
                            "content": {"type": "string", "description": "Complete content to write."},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": "Replace target text with replacement text in an existing file (shows diff and asks approval).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path to edit."},
                            "target": {"type": "string", "description": "Exact text block to find."},
                            "replacement": {"type": "string", "description": "Replacement text block."},
                        },
                        "required": ["path", "target", "replacement"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_dir",
                    "description": "List files and subdirectories in a directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Directory path (defaults to current working directory)."},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_files",
                    "description": "Search for files by filename pattern or search text inside files.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Text or regex pattern to search for."},
                            "path": {"type": "string", "description": "Directory to search in (optional)."},
                            "file_pattern": {"type": "string", "description": "Optional glob like *.py or *.md."},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_shell",
                    "description": "Execute a PowerShell command in the working directory (requires approval when auto-approve is off).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "PowerShell command to execute."},
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web and return top results with titles, snippets, and URLs.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query."},
                            "max_results": {"type": "integer", "description": "Number of results (default 5)."},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_url",
                    "description": "Fetch a web page or JSON endpoint and extract clean readable content.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "Full HTTP/HTTPS URL."},
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "manage_tasks",
                    "description": "Personal assistant task manager: add, list, complete, or delete tasks.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["add", "list", "complete", "delete"]},
                            "title": {"type": "string", "description": "Task title (for add)."},
                            "task_id": {"type": "integer", "description": "Task ID (for complete/delete)."},
                            "priority": {"type": "string", "description": "low, normal, or high."},
                            "due_date": {"type": "string", "description": "Optional due date."},
                        },
                        "required": ["action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "manage_notes",
                    "description": "Personal assistant notes: add, list, or search saved notes.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["add", "list", "search"]},
                            "title": {"type": "string", "description": "Note title."},
                            "content": {"type": "string", "description": "Note body content."},
                            "query": {"type": "string", "description": "Search query."},
                        },
                        "required": ["action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "analyze_data",
                    "description": "Perform data analysis on CSV or Excel (.xlsx) files: summary stats, columns, missing values, and sample rows.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Path to .csv or .xlsx file."},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "schedule_reminder",
                    "description": "Schedule a smart reminder using natural language (Arabic or English) or list active reminders.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["add", "list"]},
                            "text": {"type": "string", "description": "Natural language reminder e.g. 'remind me tomorrow at 5 to call Ahmed'."},
                        },
                        "required": ["action"],
                    },
                },
            },
        ]
        for schema, _ in self._extra_tools.values():
            schemas.append(schema)
        return schemas

    async def execute_tool(
        self,
        name: str,
        args: Dict[str, Any],
        auto_approve: bool,
        approval_callback: Optional[ApprovalCallback] = None,
    ) -> ToolExecutionResult:
        """Execute a named tool with approval checks for file writes/edits and shell commands."""
        try:
            if name == "read_file":
                return self._tool_read_file(args)
            if name == "write_file":
                return await self._tool_write_file(args, auto_approve, approval_callback)
            if name == "edit_file":
                return await self._tool_edit_file(args, auto_approve, approval_callback)
            if name == "list_dir":
                return self._tool_list_dir(args)
            if name == "search_files":
                return self._tool_search_files(args)
            if name == "run_shell":
                return await self._tool_run_shell(args, auto_approve, approval_callback)
            if name == "web_search":
                return await self._tool_web_search(args)
            if name == "fetch_url":
                return await self._tool_fetch_url(args)
            if name == "manage_tasks":
                return self._tool_manage_tasks(args)
            if name == "manage_notes":
                return self._tool_manage_notes(args)
            if name == "analyze_data":
                return self._tool_analyze_data(args)
            if name == "schedule_reminder":
                return self._tool_schedule_reminder(args)
            if name in self._extra_tools:
                _, handler = self._extra_tools[name]
                out = await handler(**args)
                return ToolExecutionResult(tool_name=name, success=True, output=str(out))

            return ToolExecutionResult(
                tool_name=name,
                success=False,
                output=f"Unknown tool '{name}'.",
            )
        except Exception as exc:
            return ToolExecutionResult(
                tool_name=name,
                success=False,
                output=f"Tool '{name}' raised {type(exc).__name__}: {exc}",
            )

    # ── Built-in Tool Implementations ─────────────────────────────────────────

    def _tool_read_file(self, args: Dict[str, Any]) -> ToolExecutionResult:
        target = self._resolve_path(str(args.get("path", "")))
        if not target.exists() or not target.is_file():
            return ToolExecutionResult("read_file", False, f"File not found: {target}")
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = int(args.get("start_line") or 1)
        end = int(args.get("end_line") or len(lines))
        start = max(1, start)
        end = min(len(lines), end)
        selected = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        if len(selected) > 30000:
            selected = selected[:30000] + "\n... [truncated]"
        return ToolExecutionResult("read_file", True, selected or "(empty file)")

    async def _tool_write_file(
        self,
        args: Dict[str, Any],
        auto_approve: bool,
        approval_callback: Optional[ApprovalCallback],
    ) -> ToolExecutionResult:
        rel_path = str(args.get("path", ""))
        new_content = str(args.get("content", ""))
        target = self._resolve_path(rel_path)
        old_content = ""
        if target.exists() and target.is_file():
            old_content = target.read_text(encoding="utf-8", errors="replace")

        diff_txt = build_unified_diff(old_content, new_content, rel_path)
        if not auto_approve and approval_callback is not None:
            approved = await approval_callback(
                "write_file",
                f"Write to {target}",
                diff_txt,
            )
            if not approved:
                return ToolExecutionResult(
                    "write_file",
                    False,
                    f"User rejected writing to {rel_path}.",
                    diff_or_preview=diff_txt,
                    approved=False,
                )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8")
        return ToolExecutionResult(
            "write_file",
            True,
            f"Successfully wrote {len(new_content)} bytes to {target}.",
            diff_or_preview=diff_txt,
        )

    async def _tool_edit_file(
        self,
        args: Dict[str, Any],
        auto_approve: bool,
        approval_callback: Optional[ApprovalCallback],
    ) -> ToolExecutionResult:
        rel_path = str(args.get("path", ""))
        find_text = str(args.get("target", ""))
        replace_text = str(args.get("replacement", ""))
        target = self._resolve_path(rel_path)
        if not target.exists() or not target.is_file():
            return ToolExecutionResult("edit_file", False, f"File not found: {target}")

        old_content = target.read_text(encoding="utf-8", errors="replace")
        if find_text not in old_content:
            return ToolExecutionResult(
                "edit_file",
                False,
                f"Target snippet not found in {rel_path}. Read the file first to verify exact contents.",
            )

        new_content = old_content.replace(find_text, replace_text, 1)
        diff_txt = build_unified_diff(old_content, new_content, rel_path)

        if not auto_approve and approval_callback is not None:
            approved = await approval_callback(
                "edit_file",
                f"Edit {target}",
                diff_txt,
            )
            if not approved:
                return ToolExecutionResult(
                    "edit_file",
                    False,
                    f"User rejected editing {rel_path}.",
                    diff_or_preview=diff_txt,
                    approved=False,
                )

        target.write_text(new_content, encoding="utf-8")
        return ToolExecutionResult(
            "edit_file",
            True,
            f"Successfully edited {target}.",
            diff_or_preview=diff_txt,
        )

    def _tool_list_dir(self, args: Dict[str, Any]) -> ToolExecutionResult:
        raw_path = str(args.get("path") or ".")
        target = self._resolve_path(raw_path)
        if not target.exists() or not target.is_dir():
            return ToolExecutionResult("list_dir", False, f"Directory not found: {target}")

        items: List[str] = []
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.is_dir():
                items.append(f"[DIR]  {entry.name}/")
            else:
                sz = entry.stat().st_size
                items.append(f"[FILE] {entry.name} ({sz} bytes)")
        return ToolExecutionResult(
            "list_dir",
            True,
            f"Contents of {target}:\n" + ("\n".join(items[:250]) if items else "(empty directory)"),
        )

    def _tool_search_files(self, args: Dict[str, Any]) -> ToolExecutionResult:
        query = str(args.get("query", "")).strip()
        base = self._resolve_path(str(args.get("path") or "."))
        glob_pat = str(args.get("file_pattern") or "*")
        if not query:
            return ToolExecutionResult("search_files", False, "Empty search query.")

        matches: List[str] = []
        try:
            rx = re.compile(query, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(query), re.IGNORECASE)

        skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in files:
                fpath = Path(root) / fname
                if glob_pat != "*" and not fpath.match(glob_pat):
                    continue
                rel = fpath.relative_to(base) if fpath.is_relative_to(base) else fpath
                if rx.search(fname):
                    matches.append(f"[FILENAME] {rel}")
                try:
                    if fpath.stat().st_size <= 512_000:
                        txt = fpath.read_text(encoding="utf-8", errors="ignore")
                        for idx, line in enumerate(txt.splitlines(), 1):
                            if rx.search(line):
                                matches.append(f"{rel}:{idx}: {line.strip()[:140]}")
                                if len(matches) >= 80:
                                    break
                except Exception:
                    pass
                if len(matches) >= 80:
                    break
            if len(matches) >= 80:
                break

        if not matches:
            return ToolExecutionResult("search_files", True, f"No matches found for '{query}'.")
        return ToolExecutionResult("search_files", True, "\n".join(matches))

    async def _tool_run_shell(
        self,
        args: Dict[str, Any],
        auto_approve: bool,
        approval_callback: Optional[ApprovalCallback],
    ) -> ToolExecutionResult:
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return ToolExecutionResult("run_shell", False, "No command provided.")

        preview = f"PowerShell (cwd: {self.cwd}):\n> {cmd}"
        if not auto_approve and approval_callback is not None:
            approved = await approval_callback(
                "run_shell",
                "Execute PowerShell Command",
                preview,
            )
            if not approved:
                return ToolExecutionResult(
                    "run_shell",
                    False,
                    "User rejected PowerShell execution.",
                    diff_or_preview=preview,
                    approved=False,
                )

        def _exec() -> Tuple[int, str, str]:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                cwd=str(self.cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            return proc.returncode, proc.stdout, proc.stderr

        code, stdout, stderr = await asyncio.to_thread(_exec)
        combined = f"Exit Code: {code}\n"
        if stdout.strip():
            combined += f"STDOUT:\n{stdout.strip()}\n"
        if stderr.strip():
            combined += f"STDERR:\n{stderr.strip()}\n"
        return ToolExecutionResult(
            "run_shell",
            code == 0,
            combined.strip()[:16000],
            diff_or_preview=preview,
        )

    async def _tool_web_search(self, args: Dict[str, Any]) -> ToolExecutionResult:
        query = str(args.get("query", "")).strip()
        max_results = int(args.get("max_results") or 5)
        if not query:
            return ToolExecutionResult("web_search", False, "Missing search query.")

        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            soup = BeautifulSoup(resp.text, "html.parser")
            results: List[str] = []
            for item in soup.select(".result"):
                title_el = item.select_one(".result__title")
                snip_el = item.select_one(".result__snippet")
                link_el = item.select_one(".result__url")
                if title_el:
                    t = title_el.get_text(" ", strip=True)
                    s = snip_el.get_text(" ", strip=True) if snip_el else ""
                    u = link_el.get_text(" ", strip=True) if link_el else ""
                    results.append(f"- {t}\n  URL: {u}\n  Snippet: {s}")
                if len(results) >= max_results:
                    break

        if not results:
            return ToolExecutionResult("web_search", True, f"No web results parsed for '{query}'.")
        return ToolExecutionResult("web_search", True, "\n\n".join(results))

    async def _tool_fetch_url(self, args: Dict[str, Any]) -> ToolExecutionResult:
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CloudAgent/1.0"
        }
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            ctype = resp.headers.get("content-type", "")
            if "application/json" in ctype:
                return ToolExecutionResult("fetch_url", True, resp.text[:16000])
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            return ToolExecutionResult("fetch_url", True, text[:16000] or "(empty page)")

    def _tool_manage_tasks(self, args: Dict[str, Any]) -> ToolExecutionResult:
        action = str(args.get("action", "list")).lower()
        if action == "add":
            title = str(args.get("title", "")).strip()
            if not title:
                return ToolExecutionResult("manage_tasks", False, "Task title is required.")
            prio = str(args.get("priority") or "normal")
            due = str(args.get("due_date") or "")
            tid = self.storage.add_task(title, prio, due)
            return ToolExecutionResult("manage_tasks", True, f"Task #{tid} added: '{title}' [{prio}]")
        if action == "complete":
            tid = int(args.get("task_id") or 0)
            ok = self.storage.complete_task(tid)
            return ToolExecutionResult("manage_tasks", ok, f"Task #{tid} marked completed." if ok else f"Task #{tid} not found.")
        if action == "delete":
            tid = int(args.get("task_id") or 0)
            ok = self.storage.delete_task(tid)
            return ToolExecutionResult("manage_tasks", ok, f"Task #{tid} deleted." if ok else f"Task #{tid} not found.")

        tasks = self.storage.list_tasks(include_completed=True)
        if not tasks:
            return ToolExecutionResult("manage_tasks", True, "No tasks found.")
        lines = [
            f"#{t['id']} [{'x' if t['status'] == 'done' else ' '}] {t['title']} (priority: {t['priority']})"
            + (f" due: {t['due_date']}" if t["due_date"] else "")
            for t in tasks
        ]
        return ToolExecutionResult("manage_tasks", True, "\n".join(lines))

    def _tool_manage_notes(self, args: Dict[str, Any]) -> ToolExecutionResult:
        action = str(args.get("action", "list")).lower()
        if action == "add":
            title = str(args.get("title") or "Untitled Note")
            content = str(args.get("content") or "")
            nid = self.storage.add_note(title, content)
            return ToolExecutionResult("manage_notes", True, f"Saved note #{nid}: {title}")
        query = str(args.get("query") or "") if action == "search" else ""
        notes = self.storage.list_notes(query=query)
        if not notes:
            return ToolExecutionResult("manage_notes", True, "No notes found.")
        lines = [f"#{n['id']} [{n['created_at']}] {n['title']}: {n['content']}" for n in notes]
        return ToolExecutionResult("manage_notes", True, "\n".join(lines))

    def _tool_analyze_data(self, args: Dict[str, Any]) -> ToolExecutionResult:
        target = self._resolve_path(str(args.get("path", "")))
        if not target.exists():
            return ToolExecutionResult("analyze_data", False, f"Data file not found: {target}")

        rows: List[Dict[str, Any]] = []
        headers: List[str] = []
        ext = target.suffix.lower()

        if ext == ".csv":
            with target.open("r", encoding="utf-8", errors="replace", newline="") as f:
                reader = csv.DictReader(f)
                headers = list(reader.fieldnames or [])
                for r in reader:
                    rows.append(dict(r))
        elif ext in (".xlsx", ".xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(target, read_only=True, data_only=True)
            ws = wb.active
            all_rows = list(ws.iter_rows(values_only=True))
            wb.close()
            if all_rows:
                headers = [str(c) if c is not None else f"col_{i}" for i, c in enumerate(all_rows[0])]
                for r in all_rows[1:]:
                    rows.append({headers[i]: r[i] for i in range(min(len(headers), len(r)))})
        else:
            return ToolExecutionResult("analyze_data", False, f"Unsupported file extension '{ext}'. Use .csv or .xlsx.")

        total_rows = len(rows)
        report: List[str] = [
            f"Data Analysis Report for: {target.name}",
            f"Total Rows: {total_rows} | Total Columns: {len(headers)}",
            f"Columns: {', '.join(headers)}",
            "\nColumn Statistics:",
        ]

        for col in headers:
            vals = [r.get(col) for r in rows]
            non_empty = [v for v in vals if v is not None and str(v).strip() != ""]
            missing = total_rows - len(non_empty)
            nums: List[float] = []
            for v in non_empty:
                try:
                    nums.append(float(str(v).replace(",", "")))
                except ValueError:
                    pass
            if nums and len(nums) >= len(non_empty) * 0.7:
                avg = sum(nums) / len(nums)
                report.append(
                    f"  - {col} [numeric]: count={len(nums)}, missing={missing}, "
                    f"min={min(nums):.2f}, max={max(nums):.2f}, mean={avg:.2f}, sum={sum(nums):.2f}"
                )
            else:
                unique_cnt = len(set(str(v) for v in non_empty))
                report.append(f"  - {col} [text]: non-null={len(non_empty)}, missing={missing}, unique={unique_cnt}")

        sample = rows[:5]
        report.append("\nSample Rows (first 5):")
        report.append(json.dumps(sample, ensure_ascii=False, indent=2))
        return ToolExecutionResult("analyze_data", True, "\n".join(report))

    def _tool_schedule_reminder(self, args: Dict[str, Any]) -> ToolExecutionResult:
        action = str(args.get("action", "list")).lower()
        if action == "add":
            txt = str(args.get("text", "")).strip()
            if not txt:
                return ToolExecutionResult("schedule_reminder", False, "Reminder text is required.")
            if self.reminder_callback:
                msg = self.reminder_callback("add", txt)
                return ToolExecutionResult("schedule_reminder", True, msg)
        reminders = self.storage.list_reminders(active_only=True)
        if not reminders:
            return ToolExecutionResult("schedule_reminder", True, "No active reminders.")
        out = [
            f"#{r['id']} [{r['trigger_time']}] {r['message']}"
            + (f" (recurring: {r['recurrence']})" if r["recurrence"] else "")
            for r in reminders
        ]
        return ToolExecutionResult("schedule_reminder", True, "\n".join(out))
