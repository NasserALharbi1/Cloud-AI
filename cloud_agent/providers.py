"""Multi-provider LLM manager with streaming, quota tracking, key testing, and auto-fallback."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

import httpx

from cloud_agent.config import AppConfig, PROVIDER_PRESETS, ProviderEntry, logger
from cloud_agent.storage import CloudStorage


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class StreamChunk:
    text: str = ""
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    quota_bar: Optional[str] = None
    fallback_notice: Optional[str] = None


def format_quota_bar(remaining: int, limit: int) -> str:
    """Format a 9-slot ASCII progress bar showing remaining requests and percentage."""
    if limit <= 0:
        return "[---------] 0/0 left (0%)"
    ratio = max(0.0, min(1.0, remaining / limit))
    filled = int(round(ratio * 9))
    bar = "#" * filled + "-" * (9 - filled)
    pct = int(round(ratio * 100))
    return f"[{bar}] {remaining}/{limit} left ({pct}%)"


def _build_quota_bar_from_headers(headers: httpx.Headers, fallback_used: int = 0, default_limit: int = 50) -> str:
    """Parse rate-limit headers from OpenAI/Groq/OpenRouter/Anthropic into a visual bar."""
    rem = (
        headers.get("x-ratelimit-remaining-requests")
        or headers.get("x-ratelimit-remaining-tokens")
        or headers.get("anthropic-ratelimit-requests-remaining")
    )
    lim = (
        headers.get("x-ratelimit-limit-requests")
        or headers.get("x-ratelimit-limit-tokens")
        or headers.get("anthropic-ratelimit-requests-limit")
    )
    if rem is not None and lim is not None:
        try:
            rem_i = int(float(rem))
            lim_i = int(float(lim))
            if lim_i > 0:
                return format_quota_bar(rem_i, lim_i)
        except ValueError:
            pass
    rem_est = max(0, default_limit - fallback_used)
    return format_quota_bar(rem_est, default_limit)


class ProviderManager:
    """Manages LLM providers, encrypted keys, model statuses, counters, and auto-fallback."""

    def __init__(self, config: AppConfig, storage: CloudStorage) -> None:
        self.config = config
        self.storage = storage

    # ── Provider CRUD ─────────────────────────────────────────────────────────

    def add_or_update_provider(
        self,
        name: str,
        base_url: str,
        api_key: str,
        models: List[str],
        display_name: Optional[str] = None,
        api_format: Optional[str] = None,
        is_free: Optional[bool] = None,
    ) -> ProviderEntry:
        key_id = name.strip().lower()
        preset = PROVIDER_PRESETS.get(key_id, {})
        resolved_base = base_url.strip() or preset.get("base_url", "https://api.openai.com/v1")
        resolved_base = resolved_base.rstrip("/")
        resolved_fmt = api_format or preset.get("api_format") or (
            "anthropic" if "anthropic.com" in resolved_base else "openai"
        )
        resolved_display = display_name or preset.get("display_name") or name.strip()
        resolved_models = [m.strip() for m in models if m.strip()] or list(preset.get("default_models", []))
        resolved_free = (
            is_free
            if is_free is not None
            else bool(
                preset.get("is_free", False)
                or any(":free" in m for m in resolved_models)
                or "localhost" in resolved_base
                or "127.0.0.1" in resolved_base
            )
        )
        requires_key = bool(preset.get("requires_key", "localhost" not in resolved_base))

        entry = ProviderEntry(
            name=key_id,
            display_name=resolved_display,
            base_url=resolved_base,
            api_format=resolved_fmt,
            models=resolved_models,
            is_free=resolved_free,
            requires_key=requires_key,
            model_status={m: "available" for m in resolved_models},
            quota_info={m: "[---------] quota unknown" for m in resolved_models},
        )
        self.config.providers[key_id] = entry
        if api_key.strip():
            self.config.set_provider_key(key_id, api_key.strip())

        if not self.config.active_provider or not self.config.active_model:
            self.config.active_provider = key_id
            if resolved_models:
                self.config.active_model = resolved_models[0]

        self.config.save()
        return entry

    def remove_provider(self, name: str) -> bool:
        key_id = name.strip().lower()
        if key_id not in self.config.providers:
            return False
        del self.config.providers[key_id]
        self.config.delete_provider_key(key_id)
        if self.config.active_provider == key_id:
            self.config.active_provider = ""
            self.config.active_model = ""
            for pname, pentry in self.config.providers.items():
                if pentry.models:
                    self.config.active_provider = pname
                    self.config.active_model = pentry.models[0]
                    break
        self.config.save()
        return True

    def set_active_model(self, provider_name: str, model_name: str) -> bool:
        key_id = provider_name.strip().lower()
        if key_id not in self.config.providers:
            return False
        pentry = self.config.providers[key_id]
        if model_name not in pentry.models:
            pentry.models.append(model_name)
            pentry.model_status[model_name] = "available"
            pentry.quota_info[model_name] = "[---------] quota unknown"
        self.config.active_provider = key_id
        self.config.active_model = model_name
        self.config.save()
        return True

    # ── Status & Counters ─────────────────────────────────────────────────────

    def get_all_models(self) -> List[Dict[str, Any]]:
        """Return flat list of all configured models with status, quota, and daily request counts."""
        usage_map = self.storage.get_requests_today_by_model()
        out: List[Dict[str, Any]] = []
        for pname, pentry in self.config.providers.items():
            for m in pentry.models:
                status = pentry.model_status.get(m, "available")
                quota = pentry.quota_info.get(m, "[---------] quota unknown")
                is_free_model = pentry.is_free or ":free" in m.lower() or "localhost" in pentry.base_url
                out.append(
                    {
                        "provider": pname,
                        "display_provider": pentry.display_name,
                        "model": m,
                        "status": status,
                        "quota": quota,
                        "is_free": is_free_model,
                        "requests_today": usage_map.get(f"{pname}/{m}", 0),
                        "is_active": (pname == self.config.active_provider and m == self.config.active_model),
                    }
                )
        return out

    def get_status_counters(self) -> Tuple[int, int, int]:
        """Return (free_models_count, unavailable_count, requests_today_total)."""
        models = self.get_all_models()
        free_count = sum(1 for m in models if m["is_free"] and m["status"] == "available")
        unavail_count = sum(1 for m in models if m["status"] != "available")
        reqs_today = self.storage.get_requests_today_total()
        return free_count, unavail_count, reqs_today

    def get_active_quota_bar(self) -> str:
        p = self.config.providers.get(self.config.active_provider)
        if not p:
            return "[---------] no model"
        raw = p.quota_info.get(self.config.active_model, "")
        if not raw or "unknown" in raw:
            usage_map = self.storage.get_requests_today_by_model()
            used = usage_map.get(f"{self.config.active_provider}/{self.config.active_model}", 0)
            lim = 50 if "pro" in self.config.active_model.lower() or "openrouter" in self.config.active_provider else 250
            return format_quota_bar(max(0, lim - used), lim)
        return raw

    async def refresh_live_usage(self) -> str:
        """Query live usage endpoints (e.g. OpenRouter /api/v1/key) and compute remaining daily quotas."""
        usage_map = self.storage.get_requests_today_by_model()
        summary_lines: List[str] = []

        # 1. OpenRouter live daily free-tier & credit quota via GET /api/v1/key
        or_entry = self.config.providers.get("openrouter")
        if or_entry:
            or_key = self.config.get_provider_key("openrouter") or ""
            if or_key:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.get(
                            f"{or_entry.base_url.rstrip('/')}/key",
                            headers={"Authorization": f"Bearer {or_key}"},
                        )
                        if resp.status_code == 200:
                            data = resp.json().get("data") or {}
                            free_info = data.get("free_model_daily_requests") or {}
                            if free_info and "remaining" in free_info and "limit" in free_info:
                                rem = int(free_info["remaining"])
                                lim = int(free_info["limit"])
                                used = int(free_info.get("used", lim - rem))
                                bar = format_quota_bar(rem, lim)
                                for m in or_entry.models:
                                    if ":free" in m.lower():
                                        or_entry.quota_info[m] = bar
                                        if rem <= 0:
                                            or_entry.model_status[m] = "quota_exceeded"
                                summary_lines.append(
                                    f"OpenRouter Free Tier: {rem}/{lim} requests left today (used {used}) {bar}"
                                )
                except Exception as exc:
                    logger.warning("OpenRouter live usage check failed: %s", exc)

        # 2. Update estimated daily quota bars for Gemini / other providers
        for pname, pentry in self.config.providers.items():
            for m in pentry.models:
                used = usage_map.get(f"{pname}/{m}", 0)
                cur_q = pentry.quota_info.get(m, "")
                st = pentry.model_status.get(m, "available")
                if st == "quota_exceeded":
                    lim = 50 if "pro" in m.lower() or pname == "openrouter" else 250
                    pentry.quota_info[m] = format_quota_bar(0, lim)
                elif not cur_q or "unknown" in cur_q or "ready" in cur_q:
                    lim = 50 if "pro" in m.lower() or pname == "openrouter" else 250
                    rem = max(0, lim - used)
                    pentry.quota_info[m] = format_quota_bar(rem, lim)
                summary_lines.append(
                    f"{pname}/{m} [{pentry.model_status.get(m, 'available')}]: {pentry.quota_info.get(m)} (today: {used})"
                )

        self.config.save()
        return "\n".join(summary_lines)

    # ── Key Validation on Save ────────────────────────────────────────────────

    async def test_provider_connection(
        self,
        provider_name: str,
        base_url: str,
        api_key: str,
        model: str,
        api_format: str = "openai",
    ) -> Tuple[bool, str, str]:
        """Test an API key and endpoint with a minimal request. Returns (ok, message, quota_bar)."""
        base = base_url.rstrip("/")
        headers: Dict[str, str] = {"Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                if api_format == "anthropic" or "anthropic.com" in base:
                    headers["x-api-key"] = api_key
                    headers["anthropic-version"] = "2023-06-01"
                    resp = await client.post(
                        f"{base}/messages",
                        headers=headers,
                        json={
                            "model": model,
                            "max_tokens": 4,
                            "messages": [{"role": "user", "content": "ping"}],
                        },
                    )
                else:
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                    resp = await client.post(
                        f"{base}/chat/completions",
                        headers=headers,
                        json={
                            "model": model,
                            "max_tokens": 4,
                            "messages": [{"role": "user", "content": "ping"}],
                        },
                    )

                quota_bar = _build_quota_bar_from_headers(resp.headers)
                if resp.status_code in (200, 201):
                    return True, f"Connected ({model})", quota_bar
                if resp.status_code == 429:
                    return False, "Rate limit / quota exceeded (HTTP 429)", "[---------] 0% quota"
                if resp.status_code in (401, 403):
                    return False, f"Authentication failed (HTTP {resp.status_code})", "[---------] unauthorized"
                err_txt = resp.text[:160]
                return False, f"HTTP {resp.status_code}: {err_txt}", quota_bar
        except Exception as exc:
            return False, f"Connection error: {type(exc).__name__}: {exc}", "[---------] unreachable"

    # ── Auto-Fallback Selection ───────────────────────────────────────────────

    def _find_next_available_model(self, exclude: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
        """Find the next available (provider, model) pair not in exclude list."""
        for pname, pentry in self.config.providers.items():
            for m in pentry.models:
                if (pname, m) in exclude:
                    continue
                if pentry.model_status.get(m, "available") == "available":
                    return pname, m
        return None

    def mark_model_status(self, provider: str, model: str, status: str, quota: Optional[str] = None) -> None:
        p = self.config.providers.get(provider)
        if not p:
            return
        p.model_status[model] = status
        if quota:
            p.quota_info[model] = quota
        self.config.save()

    # ── Streaming Chat + Tool Calls with Auto-Fallback ────────────────────────

    async def stream_chat_with_fallback(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Stream response from active model; automatically switch to fallback model on error."""
        if not self.config.active_provider or not self.config.active_model:
            raise RuntimeError("No active model configured. Use /provider add or press F3 to select a model.")

        tried: List[Tuple[str, str]] = []
        current_provider = self.config.active_provider
        current_model = self.config.active_model

        while True:
            tried.append((current_provider, current_model))
            pentry = self.config.providers.get(current_provider)
            if not pentry:
                raise RuntimeError(f"Provider '{current_provider}' not found in config.")

            api_key = self.config.get_provider_key(current_provider) or ""
            if pentry.requires_key and not api_key:
                self.mark_model_status(current_provider, current_model, "unavailable", "[---------] missing key")
                nxt = self._find_next_available_model(tried)
                if not nxt:
                    raise RuntimeError(
                        f"API key missing for '{pentry.display_name}' and no fallback models available."
                    )
                old_label = f"{current_provider}/{current_model}"
                current_provider, current_model = nxt
                self.set_active_model(current_provider, current_model)
                yield StreamChunk(
                    fallback_notice=f"Switched from {old_label} to {current_provider}/{current_model} (missing API key)."
                )
                continue

            try:
                self.storage.increment_model_request(current_provider, current_model)
                if pentry.api_format == "anthropic" or "anthropic.com" in pentry.base_url:
                    async for chunk in self._stream_anthropic(pentry, api_key, current_model, messages, tools_schema):
                        yield chunk
                else:
                    async for chunk in self._stream_openai(pentry, api_key, current_model, messages, tools_schema):
                        yield chunk
                self.mark_model_status(current_provider, current_model, "available")
                return
            except Exception as exc:
                logger.warning("Model %s/%s failed: %s", current_provider, current_model, exc)
                err_str = str(exc)
                status_tag = "quota_exceeded" if "429" in err_str or "quota" in err_str.lower() else "unavailable"
                quota_tag = "[---------] 0% quota" if status_tag == "quota_exceeded" else "[---------] error"
                self.mark_model_status(current_provider, current_model, status_tag, quota_tag)

                nxt = self._find_next_available_model(tried)
                if not nxt:
                    raise RuntimeError(f"Model {current_provider}/{current_model} failed ({exc}) and no fallback available.") from exc

                old_label = f"{current_provider}/{current_model}"
                current_provider, current_model = nxt
                self.set_active_model(current_provider, current_model)
                yield StreamChunk(
                    fallback_notice=(
                        f"Model {old_label} failed ({type(exc).__name__}). "
                        f"Auto-switched to {current_provider}/{current_model}."
                    )
                )

    async def _stream_openai(
        self,
        pentry: ProviderEntry,
        api_key: str,
        model: str,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
    ) -> AsyncGenerator[StreamChunk, None]:
        """Stream from an OpenAI-compatible /chat/completions endpoint."""
        url = f"{pentry.base_url.rstrip('/')}/chat/completions"
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if "openrouter.ai" in url:
            headers["HTTP-Referer"] = "https://cloud-agent.local"
            headers["X-Title"] = "Cloud CLI Agent"

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if tools_schema:
            payload["tools"] = tools_schema

        tool_acc: Dict[int, Dict[str, str]] = {}

        async with httpx.AsyncClient(timeout=90.0) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code == 400 and tools_schema:
                    # Some free/local endpoints reject 'tools' parameter; retry without native tools
                    # (agent prompt already supports <tool_call> XML fallback)
                    pass
                if resp.status_code >= 400:
                    body = await resp.aread()
                    body_text = body.decode("utf-8", errors="replace")
                    if resp.status_code == 400 and tools_schema and ("tool" in body_text.lower() or "function" in body_text.lower()):
                        # Retry immediately without native tools parameter
                        payload.pop("tools", None)
                        async with client.stream("POST", url, headers=headers, json=payload) as retry_resp:
                            if retry_resp.status_code >= 400:
                                rbody = await retry_resp.aread()
                                raise RuntimeError(f"HTTP {retry_resp.status_code}: {rbody.decode('utf-8', errors='replace')[:200]}")
                            qbar = _build_quota_bar_from_headers(retry_resp.headers)
                            self.mark_model_status(pentry.name, model, "available", qbar)
                            yield StreamChunk(quota_bar=qbar)
                            async for line in retry_resp.aiter_lines():
                                chunk = self._parse_openai_sse_line(line, tool_acc)
                                if chunk:
                                    yield chunk
                            return
                    raise RuntimeError(f"HTTP {resp.status_code}: {body_text[:200]}")

                used_today = self.storage.get_requests_today_by_model().get(f"{pentry.name}/{model}", 0)
                def_lim = 50 if ("pro" in model.lower() or pentry.name == "openrouter") else 250
                qbar = _build_quota_bar_from_headers(resp.headers, fallback_used=used_today, default_limit=def_lim)
                self.mark_model_status(pentry.name, model, "available", qbar)
                if pentry.name == "openrouter" and ":free" in model.lower():
                    # Refresh exact OpenRouter free-tier counter in background
                    try:
                        await self.refresh_live_usage()
                        qbar = pentry.quota_info.get(model, qbar)
                    except Exception:
                        pass
                yield StreamChunk(quota_bar=qbar)

                async for line in resp.aiter_lines():
                    chunk = self._parse_openai_sse_line(line, tool_acc)
                    if chunk:
                        yield chunk

        if tool_acc:
            final_calls: List[ToolCallRequest] = []
            for idx in sorted(tool_acc.keys()):
                item = tool_acc[idx]
                raw_args = item.get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    parsed_args = {"raw": raw_args}
                if item.get("name"):
                    final_calls.append(
                        ToolCallRequest(
                            id=item.get("id") or f"call_{idx}",
                            name=item["name"],
                            arguments=parsed_args,
                        )
                    )
            if final_calls:
                yield StreamChunk(tool_calls=final_calls)

    def _parse_openai_sse_line(
        self, line: str, tool_acc: Dict[int, Dict[str, str]]
    ) -> Optional[StreamChunk]:
        line = line.strip()
        if not line or not line.startswith("data:"):
            return None
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            return None
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return None
        choices = data.get("choices") or []
        if not choices:
            return None
        delta = choices[0].get("delta") or {}
        content = delta.get("content") or ""
        tcalls = delta.get("tool_calls") or []
        for tc in tcalls:
            idx = int(tc.get("index", 0))
            if idx not in tool_acc:
                tool_acc[idx] = {"id": tc.get("id", f"call_{idx}"), "name": "", "arguments": ""}
            if tc.get("id"):
                tool_acc[idx]["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                tool_acc[idx]["name"] += fn["name"]
            if fn.get("arguments"):
                tool_acc[idx]["arguments"] += fn["arguments"]
        if content:
            return StreamChunk(text=content)
        return None

    async def _stream_anthropic(
        self,
        pentry: ProviderEntry,
        api_key: str,
        model: str,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
    ) -> AsyncGenerator[StreamChunk, None]:
        """Stream from Anthropic /v1/messages endpoint."""
        url = f"{pentry.base_url.rstrip('/')}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        sys_parts: List[str] = []
        anth_msgs: List[Dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system":
                sys_parts.append(str(m.get("content", "")))
            elif role == "tool":
                anth_msgs.append(
                    {
                        "role": "user",
                        "content": f"[Tool Result ({m.get('name', 'tool')})]:\n{m.get('content', '')}",
                    }
                )
            else:
                anth_msgs.append(
                    {
                        "role": "assistant" if role == "assistant" else "user",
                        "content": str(m.get("content", "") or "."),
                    }
                )

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "messages": anth_msgs,
            "stream": True,
        }
        if sys_parts:
            payload["system"] = "\n\n".join(sys_parts)

        async with httpx.AsyncClient(timeout=90.0) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise RuntimeError(f"Anthropic HTTP {resp.status_code}: {body.decode('utf-8', errors='replace')[:200]}")

                used_today = self.storage.get_requests_today_by_model().get(f"{pentry.name}/{model}", 0)
                def_lim = 50 if ("pro" in model.lower() or pentry.name == "openrouter") else 250
                qbar = _build_quota_bar_from_headers(resp.headers, fallback_used=used_today, default_limit=def_lim)
                self.mark_model_status(pentry.name, model, "available", qbar)
                if pentry.name == "openrouter" and ":free" in model.lower():
                    # Refresh exact OpenRouter free-tier counter in background
                    try:
                        await self.refresh_live_usage()
                        qbar = pentry.quota_info.get(model, qbar)
                    except Exception:
                        pass
                yield StreamChunk(quota_bar=qbar)

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") == "content_block_delta":
                        delta = evt.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield StreamChunk(text=delta["text"])
