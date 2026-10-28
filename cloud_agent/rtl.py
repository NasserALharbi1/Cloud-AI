"""Arabic RTL detection, shaping, mixed-LTR token protection, and terminal font capability check."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import re
from typing import Dict, List, Tuple

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    HAS_RTL_LIBS = True
except ImportError:
    HAS_RTL_LIBS = False

ARABIC_RANGE_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
LATIN_RANGE_RE = re.compile(r"[A-Za-z]")

# Inline LTR tokens that must never be reshaped (inline code, @file mentions, URLs, file paths)
LTR_PROTECTED_RE = re.compile(
    r"(`[^`]+`|@[A-Za-z0-9_./\\-]+|https?://[^\s)]+|[A-Za-z]:\\[^\s]+|\b[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)"
)

# Console fonts in legacy Windows conhost that do not contain Arabic script glyphs
NON_ARABIC_CONSOLE_FONTS = {
    "consolas",
    "lucida console",
    "terminal",
    "raster fonts",
    "cascadia mono",
    "cascadia code",
}


def contains_arabic(text: str) -> bool:
    """Return True if the string contains any Arabic script characters."""
    if not text:
        return False
    return bool(ARABIC_RANGE_RE.search(text))


def detect_direction(text: str) -> str:
    """Detect primary text direction ('rtl' for Arabic-dominant/leading, 'ltr' for English)."""
    if not text or not contains_arabic(text):
        return "ltr"
    ar_count = len(ARABIC_RANGE_RE.findall(text))
    lat_count = len(LATIN_RANGE_RE.findall(text))
    # Check first strong character
    for ch in text:
        if ARABIC_RANGE_RE.match(ch):
            return "rtl"
        if LATIN_RANGE_RE.match(ch):
            break
    return "rtl" if ar_count >= lat_count else "ltr"


def shape_rtl_line(line: str, enable_shaping: bool = True) -> str:
    """Shape and reorder an Arabic line while preserving inline code, @files, and paths in LTR."""
    if not enable_shaping or not line or not contains_arabic(line):
        return line
    if os.environ.get("CLOUD_DISABLE_BIDI") == "1" or not HAS_RTL_LIBS:
        return line

    try:
        # Preserve leading Markdown structural markers ('### ', '- ', '* ', '1. ', '> ')
        prefix_match = re.match(r"^(\s*(?:#{1,6}\s+|[-*]\s+|\d+\.\s+|>\s+)?)(.*)$", line)
        if prefix_match:
            prefix, body = prefix_match.group(1), prefix_match.group(2)
        else:
            prefix, body = "", line

        if not contains_arabic(body):
            return line

        # Extract protected LTR spans and replace with neutral ASCII placeholders
        placeholders: Dict[str, str] = {}

        def _protect(match: re.Match[str]) -> str:
            token = match.group(0)
            key = f"__LTR{len(placeholders)}__"
            placeholders[key] = token
            return key

        protected_body = LTR_PROTECTED_RE.sub(_protect, body)
        reshaped = arabic_reshaper.reshape(protected_body)
        visual = str(get_display(reshaped))

        # Restore protected LTR tokens (handling both normal and reversed placeholder keys)
        for key, orig_token in placeholders.items():
            visual = visual.replace(key, orig_token)
            rev_key = key[::-1]
            visual = visual.replace(rev_key, orig_token)

        return f"{prefix}{visual}"
    except Exception:
        return line


def format_bidirectional_text(text: str, enable_shaping: bool = True) -> str:
    """Format multi-line text with Arabic reshaping & bidi support while skipping fenced code blocks."""
    if not text or not contains_arabic(text):
        return text
    lines: List[str] = text.splitlines()
    out: List[str] = []
    in_code_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            out.append(line)
            continue
        if in_code_block:
            out.append(line)
        else:
            out.append(shape_rtl_line(line, enable_shaping=enable_shaping))
    return "\n".join(out)


def check_terminal_arabic_font_support() -> Tuple[bool, str]:
    """Detect active Windows console font and whether it supports Arabic glyphs natively."""
    if os.name != "nt":
        return True, "unix-terminal"

    # Windows Terminal (wt.exe) or VS Code terminal has fallback font chaining via DirectWrite
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM") == "vscode":
        return True, "Windows Terminal / DirectWrite"

    class COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class CONSOLE_FONT_INFOEX(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.ULONG),
            ("nFont", wintypes.DWORD),
            ("dwFontSize", COORD),
            ("FontFamily", wintypes.UINT),
            ("FontWeight", wintypes.UINT),
            ("FaceName", wintypes.WCHAR * 32),
        ]

    try:
        kernel32 = ctypes.windll.kernel32
        # Open active console screen buffer CONOUT$
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        h_con = kernel32.CreateFileW(
            "CONOUT$",
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if h_con in (-1, 0, None):
            h_con = kernel32.GetStdHandle(-11)

        info = CONSOLE_FONT_INFOEX()
        info.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
        ok = kernel32.GetCurrentConsoleFontEx(h_con, False, ctypes.byref(info))
        if ok:
            face = (info.FaceName or "").strip()
            if face.lower() in NON_ARABIC_CONSOLE_FONTS or not face:
                return False, face or "Raster Fonts"
            return True, face
    except Exception:
        pass

    return False, "conhost"
