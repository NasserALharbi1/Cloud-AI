"""Configuration, paths, and Windows DPAPI + keyring secret storage for Cloud."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import keyring
except ImportError:
    keyring = None  # type: ignore


APP_SERVICE_NAME = "CloudAIAgentWindows"


def get_cloud_home() -> Path:
    """Return the path to %USERPROFILE%\\.cloud and ensure subdirectories exist."""
    user_profile = os.environ.get("USERPROFILE") or str(Path.home())
    base = Path(user_profile) / ".cloud"
    base.mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(parents=True, exist_ok=True)
    return base


CLOUD_HOME = get_cloud_home()
CONFIG_PATH = CLOUD_HOME / "config.json"
DB_PATH = CLOUD_HOME / "memory.db"
LOG_PATH = CLOUD_HOME / "logs" / "cloud.log"
DPAPI_STORE_PATH = CLOUD_HOME / "vault.dpapi.json"


def setup_logging() -> logging.Logger:
    """Configure rotating file logger in %USERPROFILE%\\.cloud\\logs\\cloud.log."""
    logger = logging.getLogger("cloud_agent")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


logger = setup_logging()


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def get_windows_clipboard() -> str:
    """Read Unicode text directly from the Windows OS Clipboard (64-bit safe Win32 API)."""
    if os.name != "nt":
        return ""
    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
    user32.OpenClipboard.restype = ctypes.wintypes.BOOL
    user32.GetClipboardData.argtypes = [ctypes.wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.wintypes.BOOL

    kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL

    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            text = ctypes.wstring_at(ptr)
            return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        finally:
            kernel32.GlobalUnlock(handle)
    except Exception:
        return ""
    finally:
        user32.CloseClipboard()


def _dpapi_encrypt(plaintext: str) -> str:
    """Encrypt string using Windows CryptProtectData (current user scope)."""
    raw = plaintext.encode("utf-8")
    if os.name != "nt":
        return base64.b64encode(raw).decode("ascii")

    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DATA_BLOB(len(raw), buf)
    blob_out = _DATA_BLOB()

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    # CRYPTPROTECT_UI_FORBIDDEN = 0x01
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "CloudAIAgentSecret",
        None,
        None,
        None,
        0x01,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise RuntimeError("Windows DPAPI CryptProtectData failed")
    try:
        encrypted_bytes = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return base64.b64encode(encrypted_bytes).decode("ascii")
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_decrypt(cipher_b64: str) -> str:
    """Decrypt base64 string using Windows CryptUnprotectData."""
    raw = base64.b64decode(cipher_b64.encode("ascii"))
    if os.name != "nt":
        return raw.decode("utf-8")

    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DATA_BLOB(len(raw), buf)
    blob_out = _DATA_BLOB()

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    # CRYPTPROTECT_UI_FORBIDDEN = 0x01
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0x01,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise RuntimeError("Windows DPAPI CryptUnprotectData failed")
    try:
        decrypted_bytes = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return decrypted_bytes.decode("utf-8")
    finally:
        kernel32.LocalFree(blob_out.pbData)


class SecretVault:
    """Encrypted secret manager using Windows Credential Locker (keyring) + DPAPI."""

    @staticmethod
    def _load_dpapi_map() -> Dict[str, str]:
        if not DPAPI_STORE_PATH.exists():
            return {}
        try:
            return json.loads(DPAPI_STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _save_dpapi_map(data: Dict[str, str]) -> None:
        DPAPI_STORE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def set_secret(cls, key_name: str, secret_value: str) -> None:
        """Store secret encrypted via keyring (Windows Vault) and DPAPI backup."""
        if not secret_value:
            cls.delete_secret(key_name)
            return
        if keyring is not None:
            try:
                keyring.set_password(APP_SERVICE_NAME, key_name, secret_value)
            except Exception as exc:
                logger.warning("Keyring store fallback to DPAPI for %s: %s", key_name, type(exc).__name__)
        store = cls._load_dpapi_map()
        store[key_name] = _dpapi_encrypt(secret_value)
        cls._save_dpapi_map(store)

    @classmethod
    def get_secret(cls, key_name: str) -> Optional[str]:
        """Retrieve decrypted secret from keyring or DPAPI vault."""
        if keyring is not None:
            try:
                val = keyring.get_password(APP_SERVICE_NAME, key_name)
                if val:
                    return val
            except Exception:
                pass
        store = cls._load_dpapi_map()
        enc = store.get(key_name)
        if not enc:
            return None
        try:
            return _dpapi_decrypt(enc)
        except Exception as exc:
            logger.error("Failed to decrypt secret %s: %s", key_name, type(exc).__name__)
            return None

    @classmethod
    def delete_secret(cls, key_name: str) -> None:
        """Remove a secret from both keyring and DPAPI vault."""
        if keyring is not None:
            try:
                keyring.delete_password(APP_SERVICE_NAME, key_name)
            except Exception:
                pass
        store = cls._load_dpapi_map()
        if key_name in store:
            del store[key_name]
            cls._save_dpapi_map(store)


PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "openai": {
        "display_name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_format": "openai",
        "default_models": ["gpt-4o", "gpt-4o-mini", "o3-mini"],
        "is_free": False,
        "requires_key": True,
    },
    "anthropic": {
        "display_name": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "api_format": "anthropic",
        "default_models": ["claude-3-7-sonnet-latest", "claude-3-5-haiku-latest"],
        "is_free": False,
        "requires_key": True,
    },
    "gemini": {
        "display_name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_format": "openai",
        "default_models": [
            "gemini-3.5-flash",
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
            "gemini-pro-latest",
            "gemini-flash-latest",
        ],
        "is_free": True,
        "requires_key": True,
    },
    "openrouter": {
        "display_name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_format": "openai",
        "default_models": [
            "google/gemma-4-31b-it:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "nvidia/nemotron-3.5-lightning:free",
            "cohere/north-mini-code:free",
            "openai/gpt-4o",
        ],
        "is_free": True,
        "requires_key": True,
    },
    "groq": {
        "display_name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_format": "openai",
        "default_models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "deepseek-r1-distill-llama-70b",
        ],
        "is_free": True,
        "requires_key": True,
    },
    "deepseek": {
        "display_name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "api_format": "openai",
        "default_models": ["deepseek-chat", "deepseek-reasoner"],
        "is_free": False,
        "requires_key": True,
    },
    "mistral": {
        "display_name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "api_format": "openai",
        "default_models": ["mistral-large-latest", "codestral-latest", "mistral-small-latest"],
        "is_free": False,
        "requires_key": True,
    },
    "ollama": {
        "display_name": "Ollama (Local)",
        "base_url": "http://localhost:11434/v1",
        "api_format": "openai",
        "default_models": ["llama3.2", "qwen2.5-coder", "deepseek-r1"],
        "is_free": True,
        "requires_key": False,
    },
}


@dataclass
class ProviderEntry:
    name: str
    display_name: str
    base_url: str
    api_format: str = "openai"  # "openai" or "anthropic"
    models: List[str] = field(default_factory=list)
    is_free: bool = False
    requires_key: bool = True
    model_status: Dict[str, str] = field(default_factory=dict)  # model -> "available" | "unavailable" | "quota_exceeded"
    quota_info: Dict[str, str] = field(default_factory=dict)    # model -> e.g. "[---------] quota unknown"


@dataclass
class AppConfig:
    active_provider: str = ""
    active_model: str = ""
    auto_approve: bool = False
    daily_briefing: bool = True
    auto_learn: bool = True
    proactive_suggestions: bool = True
    max_tool_steps: int = 12
    rtl_shaping: bool = True
    providers: Dict[str, ProviderEntry] = field(default_factory=dict)
    enabled_integrations: List[str] = field(default_factory=list)
    mcp_servers: List[Dict[str, Any]] = field(default_factory=list)

    def save(self) -> None:
        """Persist configuration to %USERPROFILE%\\.cloud\\config.json (never stores API keys)."""
        payload = {
            "active_provider": self.active_provider,
            "active_model": self.active_model,
            "auto_approve": self.auto_approve,
            "daily_briefing": self.daily_briefing,
            "auto_learn": self.auto_learn,
            "proactive_suggestions": self.proactive_suggestions,
            "max_tool_steps": self.max_tool_steps,
            "rtl_shaping": self.rtl_shaping,
            "providers": {k: asdict(v) for k, v in self.providers.items()},
            "enabled_integrations": self.enabled_integrations,
            "mcp_servers": self.mcp_servers,
        }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls) -> "AppConfig":
        """Load configuration from %USERPROFILE%\\.cloud\\config.json or return defaults."""
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            providers_map: Dict[str, ProviderEntry] = {}
            for pname, pdata in raw.get("providers", {}).items():
                providers_map[pname] = ProviderEntry(
                    name=pdata.get("name", pname),
                    display_name=pdata.get("display_name", pname),
                    base_url=pdata.get("base_url", ""),
                    api_format=pdata.get("api_format", "openai"),
                    models=list(pdata.get("models", [])),
                    is_free=bool(pdata.get("is_free", False)),
                    requires_key=bool(pdata.get("requires_key", True)),
                    model_status=dict(pdata.get("model_status", {})),
                    quota_info=dict(pdata.get("quota_info", {})),
                )
            return cls(
                active_provider=raw.get("active_provider", ""),
                active_model=raw.get("active_model", ""),
                auto_approve=bool(raw.get("auto_approve", False)),
                daily_briefing=bool(raw.get("daily_briefing", True)),
                auto_learn=bool(raw.get("auto_learn", True)),
                proactive_suggestions=bool(raw.get("proactive_suggestions", True)),
                max_tool_steps=int(raw.get("max_tool_steps", 12)),
                rtl_shaping=bool(raw.get("rtl_shaping", True)),
                providers=providers_map,
                enabled_integrations=list(raw.get("enabled_integrations", [])),
                mcp_servers=list(raw.get("mcp_servers", [])),
            )
        except Exception as exc:
            logger.error("Failed to load config.json, using default: %s", exc)
            return cls()

    def set_provider_key(self, provider_name: str, api_key: str) -> None:
        SecretVault.set_secret(f"provider_key:{provider_name.lower()}", api_key.strip())

    def get_provider_key(self, provider_name: str) -> Optional[str]:
        return SecretVault.get_secret(f"provider_key:{provider_name.lower()}")

    def delete_provider_key(self, provider_name: str) -> None:
        SecretVault.delete_secret(f"provider_key:{provider_name.lower()}")
