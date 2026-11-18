"""Лимиты подписки агента на этой машине — значок в строке сессии на сайте (владелец 17.11.2026): сколько израсходовано
окна на 5 часов и недельного и когда сброс, как /usage у Claude Code и /status у Codex.

Claude Code: GET api.anthropic.com/api/oauth/usage тем же ключом входа, что у `claude` (Linux — ~/.claude/.credentials.json,
Mac — связка ключей «Claude Code-credentials»): окна five_hour, seven_day, seven_day_opus, seven_day_sonnet — utilization
(проценты) и resets_at. Ключ не обновляем сами: истёк — Claude Code обновит его в следующей сессии.
Codex: GET chatgpt.com/backend-api/wham/usage ключом из ~/.codex/auth.json; файла нет (ключ в связке ключей) или запрос
не прошёл — последние rate_limits из журнала сессий Codex (~/.codex/sessions/**/*.jsonl, событие token_count).

Ответ: {agent, windows: [{type, used (0–100), resets_at (unix), span (сек)}], plan, at, error?}; error — not_logged_in |
expired | unavailable. Ключи наружу не уходят. Кэш — CACHE_SEC.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import platform
import time
from datetime import datetime

import aiohttp

from . import agents

CACHE_SEC = 60
CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
SPANS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400, "seven_day_opus": 7 * 86400, "seven_day_sonnet": 7 * 86400}

_cache: dict[str, tuple[float, dict]] = {}


async def get(agent: str, fresh: bool = False) -> dict:
    hit = _cache.get(agent)
    if hit and not fresh and time.time() - hit[0] < CACHE_SEC:
        return hit[1]
    try:
        out = await (_claude() if agent == "claude" else _codex())
    except Exception:                       # сеть, формат ответа — значок покажет «нет данных», мост живёт дальше
        out = {"windows": [], "error": "unavailable"}
    out = {"agent": agent, "at": time.time(), "plan": "", **out}
    _cache[agent] = (time.time(), out)
    return out


def _ts(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e12 else 1)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ── Claude Code ──────────────────────────────────────────────────────────────

async def _claude_creds() -> dict | None:
    path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    raw = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    elif platform.system() == "Darwin":
        proc = await asyncio.create_subprocess_exec("security", "find-generic-password", "-s", "Claude Code-credentials", "-w",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
        raw = out.decode().strip() if proc.returncode == 0 else None
    if not raw:
        return None
    return (json.loads(raw) or {}).get("claudeAiOauth") or None


async def _claude() -> dict:
    creds = await _claude_creds()
    if not creds or not creds.get("accessToken"):
        return {"windows": [], "error": "not_logged_in"}
    plan = creds.get("subscriptionType") or ""
    if (creds.get("expiresAt") or 0) / 1000 < time.time():
        return {"windows": [], "plan": plan, "error": "expired"}
    headers = {"Authorization": "Bearer " + creds["accessToken"], "anthropic-beta": "oauth-2025-04-20",
               "User-Agent": "yune-bridge", "Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(CLAUDE_URL, headers=headers) as r:
            if r.status in (401, 403):
                return {"windows": [], "plan": plan, "error": "expired"}
            data = await r.json(content_type=None) if r.status == 200 else None
    if not isinstance(data, dict):
        return {"windows": [], "plan": plan, "error": "unavailable"}
    windows = []
    for kind, span in SPANS.items():
        w = data.get(kind)
        if isinstance(w, dict) and w.get("utilization") is not None:
            windows.append({"type": kind, "used": float(w["utilization"]), "resets_at": _ts(w.get("resets_at")), "span": span})
    return {"windows": windows, "plan": plan}


# ── Codex ────────────────────────────────────────────────────────────────────

def _codex_kind(span: float | None) -> str:
    if span and span <= 6 * 3600:
        return "five_hour"
    return "seven_day" if span and span >= 6 * 86400 else "window"


def _codex_window(w: dict, now: float) -> dict | None:
    """Окно Codex из ответа wham/usage (used_percent, limit_window_seconds, reset_at | reset_after_seconds) или из
    журнала (used_percent, window_minutes, resets_at | resets_in_seconds)."""
    if not isinstance(w, dict) or w.get("used_percent") is None:
        return None
    span = w.get("limit_window_seconds") or ((w.get("window_minutes") or 0) * 60) or None
    resets = _ts(w.get("reset_at") or w.get("resets_at"))
    after = w.get("reset_after_seconds") or w.get("resets_in_seconds")
    if not resets and after is not None:
        resets = now + float(after)
    return {"type": _codex_kind(span), "used": float(w["used_percent"]), "resets_at": resets, "span": span}


async def _codex() -> dict:
    auth_path = os.path.join(agents.CODEX_HOME, "auth.json")
    if os.path.exists(auth_path):
        with open(auth_path, encoding="utf-8") as f:
            tokens = (json.load(f) or {}).get("tokens") or {}
        if tokens.get("access_token"):
            headers = {"Authorization": "Bearer " + tokens["access_token"], "User-Agent": "yune-bridge", "Accept": "application/json"}
            if tokens.get("account_id"):
                headers["ChatGPT-Account-Id"] = tokens["account_id"]
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                    async with s.get(CODEX_URL, headers=headers) as r:
                        data = await r.json(content_type=None) if r.status == 200 else None
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                data = None
            limit = (data or {}).get("rate_limit") if isinstance(data, dict) else None
            if isinstance(limit, dict):
                now = time.time()
                windows = [w for w in (_codex_window(limit.get(k), now) for k in ("primary_window", "secondary_window")) if w]
                if windows:
                    return {"windows": windows, "plan": str(data.get("plan_type") or "")}
    found = await asyncio.to_thread(_codex_from_log)
    if found:
        return found
    return {"windows": [], "error": "unavailable" if os.path.exists(auth_path) else "not_logged_in"}


def _codex_from_log() -> dict | None:
    """Последние rate_limits из журналов сессий Codex (свежие файлы — с конца)."""
    files = sorted(glob.glob(os.path.join(agents.CODEX_HOME, "sessions", "**", "*.jsonl"), recursive=True),
                   key=lambda p: os.path.getmtime(p), reverse=True)[:20]
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f if '"rate_limits"' in ln]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            limits = (row.get("payload") or {}).get("rate_limits") or row.get("rate_limits")
            if not isinstance(limits, dict):
                continue
            now = _ts(row.get("timestamp")) or os.path.getmtime(path)
            windows = [w for w in (_codex_window(limits.get(k), now) for k in ("primary", "secondary")) if w]
            if windows:
                return {"windows": windows, "plan": str(limits.get("plan_type") or "")}
    return None
