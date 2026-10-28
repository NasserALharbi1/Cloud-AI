"""Autonomous multi-step AI agent loop with streaming, tool execution, approvals, and self-learning."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from cloud_agent.config import AppConfig, CLOUD_HOME
from cloud_agent.memory import MemoryEngine
from cloud_agent.providers import ProviderManager, ToolCallRequest
from cloud_agent.storage import CloudStorage
from cloud_agent.tools import ApprovalCallback, ToolExecutionResult, ToolRegistry, resolve_file_mentions

INLINE_TOOL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def parse_inline_tool_calls(text: str) -> List[ToolCallRequest]:
    """Fallback parser for models that emit <tool_call>{...}</tool_call> in text."""
    calls: List[ToolCallRequest] = []
    for idx, match in enumerate(INLINE_TOOL_RE.finditer(text)):
        raw_json = match.group(1)
        try:
            obj = json.loads(raw_json)
            name = str(obj.get("name", ""))
            args = obj.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args)
            if name:
                calls.append(ToolCallRequest(id=f"inline_call_{idx}", name=name, arguments=args))
        except Exception:
            continue
    return calls


def load_cloud_md_instructions(cwd: Path) -> Tuple[str, Optional[Path]]:
    """Locate and read cloud.md (custom instructions, persona, name, style) fresh on every turn."""
    search_dirs = [
        cwd,
        Path(sys.executable).resolve().parent,
        Path(__file__).resolve().parent.parent,
        CLOUD_HOME,
    ]
    seen: set[Path] = set()
    for d in search_dirs:
        try:
            resolved_d = d.resolve()
        except Exception:
            continue
        if resolved_d in seen or not resolved_d.is_dir():
            continue
        seen.add(resolved_d)
        for candidate_name in ("cloud.md", "CLOUD.md"):
            fpath = resolved_d / candidate_name
            if fpath.is_file():
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace").strip()
                    if content:
                        return content, fpath
                except Exception:
                    pass
    return "", None


class CloudAgent:
    """Core orchestrator for conversation history, tool-use loop, memory, and approvals."""

    def __init__(
        self,
        config: AppConfig,
        storage: CloudStorage,
        provider_mgr: ProviderManager,
        tool_registry: ToolRegistry,
        memory_engine: MemoryEngine,
    ) -> None:
        self.config = config
        self.storage = storage
        self.provider_mgr = provider_mgr
        self.tool_registry = tool_registry
        self.memory_engine = memory_engine
        self.history: List[Dict[str, Any]] = []

    def clear_history(self) -> None:
        self.history.clear()

    def build_system_prompt(self, user_query: str) -> str:
        mem_block = self.memory_engine.build_memory_context(user_query)
        cwd_str = str(self.tool_registry.cwd)
        cloud_md_text, cloud_md_path = load_cloud_md_instructions(self.tool_registry.cwd)

        base = (
            "You are an autonomous terminal AI agent running on Windows.\n"
            f"Current Working Directory: {cwd_str}\n"
            "Capabilities:\n"
            "- You can read, write, edit, list, and search files, execute PowerShell commands, search the web, "
            "fetch URLs, manage tasks/notes/reminders, analyze CSV/Excel files, and use enabled integrations.\n"
            "- For multi-step engineering or personal assistant tasks: plan your steps, invoke tools, verify results, "
            "and report clearly.\n"
            "- If the endpoint does not support native function-calling schemas, you may invoke any tool by emitting:\n"
            '  <tool_call>{"name": "tool_name", "arguments": {"arg1": "val1"}}</tool_call>\n'
        )
        if cloud_md_text:
            base += (
                f"\n<cloud_md_instructions source=\"{cloud_md_path}\">\n"
                "CRITICAL PRIORITY: You MUST strictly follow the identity, name, tone, speaking style, and rules "
                "written by the user in cloud.md below. They override any default persona:\n\n"
                f"{cloud_md_text}\n"
                "</cloud_md_instructions>\n"
            )
        if mem_block:
            base += f"\n{mem_block}\n"
        return base

    async def run_turn(
        self,
        user_input: str,
        on_token: Callable[[str], None],
        on_tool_result: Callable[[str, Dict[str, Any], ToolExecutionResult], None],
        on_notice: Callable[[str], None],
        approval_callback: Optional[ApprovalCallback] = None,
    ) -> str:
        """Execute a complete user turn including @file expansion, multi-step tool loop, and learning."""
        augmented_input, attached_files = resolve_file_mentions(user_input, self.tool_registry.cwd)
        if attached_files:
            on_notice(f"Attached files: {', '.join(attached_files)}")

        sys_prompt = self.build_system_prompt(user_input)
        self.history.append({"role": "user", "content": augmented_input})

        tools_schema = self.tool_registry.get_openai_tools_schema()
        max_steps = max(1, self.config.max_tool_steps)
        final_answer_acc: List[str] = []

        for _step in range(max_steps):
            messages = [{"role": "system", "content": sys_prompt}] + self.history[-24:]
            step_text_parts: List[str] = []
            step_tool_calls: List[ToolCallRequest] = []

            async for chunk in self.provider_mgr.stream_chat_with_fallback(messages, tools_schema):
                if chunk.fallback_notice:
                    on_notice(chunk.fallback_notice)
                if chunk.text:
                    step_text_parts.append(chunk.text)
                    on_token(chunk.text)
                if chunk.tool_calls:
                    step_tool_calls.extend(chunk.tool_calls)

            step_text = "".join(step_text_parts)
            if not step_tool_calls and "<tool_call>" in step_text:
                step_tool_calls = parse_inline_tool_calls(step_text)

            if not step_tool_calls:
                self.history.append({"role": "assistant", "content": step_text})
                final_answer_acc.append(step_text)
                break

            # Record assistant tool request and execute each tool
            self.history.append({"role": "assistant", "content": step_text or f"[Calling {len(step_tool_calls)} tool(s)]"})
            for tc in step_tool_calls:
                res = await self.tool_registry.execute_tool(
                    tc.name,
                    tc.arguments,
                    auto_approve=self.config.auto_approve,
                    approval_callback=approval_callback,
                )
                on_tool_result(tc.name, tc.arguments, res)
                self.history.append(
                    {
                        "role": "user",
                        "content": f"[Tool Output for '{tc.name}' (success={res.success})]:\n{res.output}",
                    }
                )

        # Post-turn self-learning and proactive habit detection
        if self.config.auto_learn:
            learned = await self.memory_engine.process_turn_learnings(user_input, approval_callback=approval_callback)
            if learned:
                on_notice(f"🧠 Saved to memory: {', '.join(learned)}")

        if self.config.proactive_suggestions:
            suggestion = self.memory_engine.check_proactive_suggestion(user_input)
            if suggestion:
                on_notice(suggestion)

        return "\n".join(final_answer_acc)
