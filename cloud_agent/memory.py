"""Self-learning memory extraction, context retrieval, sensitivity guard, and proactive habit detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, List, Optional, Tuple

from cloud_agent.storage import CloudStorage


@dataclass
class CandidateMemory:
    category: str
    content: str
    is_sensitive: bool


# Patterns that must NEVER be stored in memory (secrets / keys)
FORBIDDEN_SECRET_RE = re.compile(
    r"(sk-[a-zA-Z0-9_-]{16,}|AIza[0-9A-Za-z_-]{25,}|ghp_[0-9A-Za-z]{20,}|password\s*[:=]|api[_-]?key\s*[:=])",
    re.IGNORECASE,
)

# Patterns that indicate personal/sensitive facts requiring user confirmation before saving
SENSITIVE_FACT_RE = re.compile(
    r"(\b\d{9,15}\b|[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+|"
    r"\b(salary|bank|iban|credit|address|phone|passport|national id|راتب|حساب بنكي|عنواني|جوالي|رقمي)\b)",
    re.IGNORECASE,
)

# Heuristic learning extraction patterns (English & Arabic)
LEARNING_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    (
        "preference",
        re.compile(
            r"(?:i prefer|i always use|please always|always reply in|my favorite|أفضل|دايم استخدم|احب استخدم|رد علي دايم بـ?)\s+(.{4,120})",
            re.IGNORECASE,
        ),
    ),
    (
        "writing_style",
        re.compile(
            r"(?:keep responses|my writing style is|write in a|tonality should be|اسلوبي في الكتابة|خلي ردودك)\s+(.{4,120})",
            re.IGNORECASE,
        ),
    ),
    (
        "fact",
        re.compile(
            r"(?:my name is|i work as|i live in|my role is|my project is|اسمي|اشتغل|أعمل|مشروعي هو|انا مطور)\s+(.{2,120})",
            re.IGNORECASE,
        ),
    ),
    (
        "habit",
        re.compile(
            r"(?:every morning i|every day i|usually i|كل يوم الصباح|عادةً اقوم بـ?|يوميا اسوي)\s+(.{4,120})",
            re.IGNORECASE,
        ),
    ),
]

# Workflow patterns for proactive suggestions
WORKFLOW_KEYWORDS: List[Tuple[str, str, re.Pattern[str]]] = [
    (
        "daily_email_summary",
        "Daily Email Summary (تلخيص الإيميلات اليومي)",
        re.compile(r"(summarize.*email|check.*gmail|unread.*email|لخص.*ايميل|فحص.*البريد|الايميلات)", re.IGNORECASE),
    ),
    (
        "daily_tasks_review",
        "Daily Tasks & Calendar Briefing (مراجعة المهام والاجتماعات)",
        re.compile(r"(today'?s tasks|my schedule|calendar today|مهام اليوم|جدولي اليوم|اجتماعاتي)", re.IGNORECASE),
    ),
    (
        "git_status_check",
        "Project Git Status & Diff Review (فحص حالة Git للمشروع)",
        re.compile(r"(git status|check git|uncommitted changes|حالة القيت|تغييرات الكود)", re.IGNORECASE),
    ),
    (
        "csv_report_analysis",
        "Automated CSV/Excel Data Summary (تحليل تقارير البيانات)",
        re.compile(r"(analyze.*\.csv|analyze.*\.xlsx|sales report|حلل.*ملف|تحليل.*بيانات)", re.IGNORECASE),
    ),
]


class MemoryEngine:
    """Manages contextual memory retrieval, self-learning extraction, and proactive habit suggestions."""

    def __init__(self, storage: CloudStorage) -> None:
        self.storage = storage

    def build_memory_context(self, user_query: str) -> str:
        """Retrieve relevant memories from SQLite and format them for the system prompt."""
        relevant = self.storage.search_memories(user_query, limit=8)
        if not relevant:
            return ""
        lines = [f"- [{m['category']}] {m['content']}" for m in relevant]
        return "Known User Memories & Preferences:\n" + "\n".join(lines)

    def extract_candidates(self, user_text: str) -> List[CandidateMemory]:
        """Extract candidate memories from user message while blocking any raw secrets."""
        if not user_text or FORBIDDEN_SECRET_RE.search(user_text):
            return []

        candidates: List[CandidateMemory] = []
        for sentence in re.split(r"[\n.!?؟]+", user_text):
            s = sentence.strip()
            if not s or len(s) > 160:
                continue
            for category, rx in LEARNING_PATTERNS:
                m = rx.search(s)
                if m:
                    fact_text = s
                    is_sens = bool(SENSITIVE_FACT_RE.search(fact_text))
                    candidates.append(
                        CandidateMemory(
                            category=category,
                            content=fact_text,
                            is_sensitive=is_sens,
                        )
                    )
                    break
        return candidates

    async def process_turn_learnings(
        self,
        user_text: str,
        approval_callback: Optional[Callable[[str, str, str], Awaitable[bool]]] = None,
    ) -> List[str]:
        """Extract learnings after a turn; ask user before saving sensitive items."""
        saved_notices: List[str] = []
        candidates = self.extract_candidates(user_text)
        for cand in candidates:
            if cand.is_sensitive:
                if approval_callback is not None:
                    approved = await approval_callback(
                        "save_memory",
                        "Save Sensitive Memory to Local SQLite?",
                        f"Category: {cand.category}\nContent: {cand.content}",
                    )
                    if not approved:
                        continue
                else:
                    continue
            self.storage.add_memory(cand.category, cand.content, is_sensitive=cand.is_sensitive)
            saved_notices.append(f"[{cand.category}] {cand.content}")
        return saved_notices

    def check_proactive_suggestion(self, user_text: str) -> Optional[str]:
        """Detect repeated user workflows and return a proactive automation suggestion when threshold met."""
        now_hr = datetime.now().hour
        for key, desc, rx in WORKFLOW_KEYWORDS:
            if rx.search(user_text):
                triggered = self.storage.record_action_pattern(key, desc, hour=now_hr)
                if triggered:
                    hr_fmt = f"{now_hr:02d}:00"
                    return (
                        f"💡 اقتراح ذكي (Proactive Suggestion): لاحظت إنك تطلب «{desc}» بشكل متكرر وقت الساعة {hr_fmt}. "
                        f"ودك أجدول لك تذكير يومي تلقائي بهالموعد؟ اكتب: `/reminders add every day at {hr_fmt} {desc}`"
                    )
        return None
