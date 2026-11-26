"""Клиент ACP (Agent Client Protocol, agentclientprotocol.com): JSON-RPC 2.0 построчно через stdin/stdout процесса агента.

Так говорят Antigravity CLI (`agy acp`), Gemini CLI (`gemini --acp`), OpenCode (`opencode acp`), Cursor (`cursor-agent acp`), GitHub Copilot
(`copilot --acp`), Qwen Code (`qwen --acp`), Grok (`grok agent stdio`), Kiro (`kiro-cli acp`), goose (`goose acp`) —
один клиент на всех (agents.ACP — команды, acp_session.py — сессия моста). Подсмотрено у T3 Code (provider-acp, 20.11.2026).

Мы — клиент: initialize (protocolVersion 1, без fs и terminal — агент сам читает файлы и запускает команды),
session/new | session/load, session/prompt (ответ — stopReason, когда ход кончился), session/cancel (уведомление).
Агент шлёт уведомления session/update (текст, инструменты, план, расход) и просит session/request_permission — ответ
решает мост по режиму доступа сессии (acp_session). Другие просьбы агента (fs/*, terminal/*) — «нет такого метода».
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Awaitable, Callable

log = logging.getLogger("claude_bridge")

PROTOCOL = 1
CLIENT_INFO = {"name": "yune-bridge", "title": "Yune", "version": "1"}
STOP_GRACE = 5


class AcpError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class AcpProc:
    """Процесс агента и JSON-RPC поверх него. on_notify(method, params) — уведомления агента; on_request(method, params)
    → результат (dict) или AcpError — его просьбы к нам. Строки не-JSON в stdout (журнал агента) пропускаем."""

    def __init__(self, argv: list[str], cwd: str, env: dict | None,
                 on_notify: Callable[[str, dict], None],
                 on_request: Callable[[str, dict], Awaitable[dict]]):
        self.argv, self.cwd, self.env = argv, cwd, env
        self.on_notify, self.on_request = on_notify, on_request
        self.proc: asyncio.subprocess.Process | None = None
        self.waiters: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.stderr_tail: list[str] = []
        self.tasks: list[asyncio.Task] = []

    @property
    def alive(self) -> bool:
        return bool(self.proc and self.proc.returncode is None)

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024, start_new_session=os.name != "nt")
        self.tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._stderr())]

    async def _read(self) -> None:
        try:
            async for raw in self.proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "method" in msg and "id" in msg:
                    asyncio.create_task(self._serve(msg))
                elif "method" in msg:
                    try:
                        self.on_notify(msg["method"], msg.get("params") or {})
                    except Exception:  # noqa: BLE001
                        log.exception("acp notify %s", msg.get("method"))
                elif "id" in msg:
                    fut = self.waiters.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            err = msg["error"] or {}
                            fut.set_exception(AcpError(int(err.get("code") or -1), str(err.get("message") or ""),
                                                       err.get("data")))
                        else:
                            fut.set_result(msg.get("result") or {})
        finally:
            tail = "; ".join(self.stderr_tail[-3:])[:400]
            for fut in self.waiters.values():
                if not fut.done():
                    fut.set_exception(AcpError(-32099, "agent exited" + (f": {tail}" if tail else "")))
            self.waiters.clear()

    async def _stderr(self) -> None:
        async for raw in self.proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                self.stderr_tail = (self.stderr_tail + [line])[-20:]
                log.debug("acp %s: %s", os.path.basename(self.argv[0]), line[:300])

    async def _serve(self, msg: dict) -> None:
        try:
            result = await self.on_request(msg["method"], msg.get("params") or {})
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except AcpError as e:
            self._write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": e.code, "message": e.message}})
        except Exception as e:  # noqa: BLE001
            log.exception("acp request %s", msg.get("method"))
            self._write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)[:300]}})

    def _write(self, obj: dict) -> None:
        if not self.alive:
            return
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())

    async def call(self, method: str, params: dict, timeout: float | None = None) -> dict:
        if not self.alive:
            raise AcpError(-32099, "agent is not running")
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.waiters[self.next_id] = fut
        self._write({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        await self.proc.stdin.drain()
        return await (asyncio.wait_for(fut, timeout) if timeout else fut)

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        proc = self.proc
        if not proc or proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return
        try:
            await asyncio.wait_for(proc.wait(), STOP_GRACE)
        except asyncio.TimeoutError:
            try:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass


def initialize_params() -> dict:
    return {"protocolVersion": PROTOCOL, "clientInfo": CLIENT_INFO,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}


EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")


def _available_models(result: dict) -> list[dict]:
    models = result.get("models") if isinstance(result.get("models"), dict) else None
    if not models:
        return []
    return [m for m in models.get("availableModels") or [] if isinstance(m, dict) and m.get("modelId")]


def _config_option(result: dict, category: str) -> dict | None:
    for opt in result.get("configOptions") or []:
        if isinstance(opt, dict) and opt.get("category") == category:
            return opt
    return None


def _option_values(opt: dict | None) -> list[str]:
    if not opt:
        return []
    out = []
    for v in opt.get("options") or []:
        if isinstance(v, dict) and (v.get("value") or v.get("id")):
            out.append(str(v.get("value") or v.get("id")))
    return out


def models_of(result: dict) -> tuple[list[dict], str, str]:
    """Модели и уровни размышлений из ответа session/new | session/load → ([{id, name, efforts}], модель, уровень).

    Если агент отдаёт configOptions с category "model" (как agy acp — отдельно модель и thought_level), берём
    базовые модели оттуда, а доступные уровни каждой модели сверяем с models.availableModels (`<model>-<level>`),
    чтобы модель и мышление выбирались раздельно и у моделей без мышления уровней не было.
    Иначе — ACP v1 `models {availableModels, currentModelId}` без отдельных уровней."""
    m_opt = _config_option(result, "model")
    m_entries = [v for v in (m_opt.get("options") or []) if isinstance(v, dict) and (v.get("value") or v.get("id"))] if m_opt else []
    if m_entries:
        current = str(m_opt.get("currentValue") or m_opt.get("value") or "")
        t_opt = _config_option(result, "thought_level")
        cur_effort = str(t_opt.get("currentValue") or t_opt.get("value") or "") if t_opt else ""
        t_levels = _option_values(t_opt)
        avail_ids = {str(m["modelId"]) for m in _available_models(result)}
        order = list(EFFORT_ORDER) + [lv for lv in t_levels if lv not in EFFORT_ORDER]
        out = []
        matched_any = False
        for v in m_entries:
            vid = str(v.get("value") or v.get("id"))
            m_efforts = [lv for lv in order if f"{vid}-{lv}" in avail_ids] if avail_ids else list(t_levels)
            if m_efforts and avail_ids:
                matched_any = True
            out.append({"id": vid, "name": str(v.get("name") or vid), "efforts": m_efforts})
        # availableModels есть, но без суффиксов уровня — применяем общие уровни из thought_level ко всем моделям
        if avail_ids and not matched_any and t_levels:
            for item in out:
                item["efforts"] = list(t_levels)
        return out, current, cur_effort

    out, current = [], ""
    avail = _available_models(result)
    if avail:
        current = str((result.get("models") or {}).get("currentModelId") or "")
        for m in avail:
            out.append({"id": str(m["modelId"]), "name": str(m.get("name") or m["modelId"]), "efforts": []})
    return out, current, ""


def model_option_id(result: dict) -> str | None:
    """id настройки модели в configOptions (для session/set_config_option), когда models_of() брал модели оттуда."""
    opt = _config_option(result, "model")
    if opt and opt.get("id") and _option_values(opt):
        return str(opt["id"])
    return None


def effort_option_id(result: dict) -> str | None:
    """id настройки уровня размышлений в configOptions (category thought_level), если агент его отдаёт."""
    opt = _config_option(result, "thought_level")
    return str(opt["id"]) if opt and opt.get("id") else None


def split_legacy_model(model: str, found: list[dict]) -> tuple[str, str] | None:
    """Старый сохранённый ID с приклеенным уровнем (`gemini-4-neon-high`) → (`gemini-4-neon`, `high`), если в списке есть такая база."""
    if not model or any(m["id"] == model for m in found):
        return None
    for m in found:
        prefix = m["id"] + "-"
        if model.startswith(prefix):
            lv = model[len(prefix):]
            if lv in (m.get("efforts") or ()):
                return m["id"], lv
    return None


def is_auth_error(e: Exception) -> bool:
    text = str(getattr(e, "message", "") or e).lower()
    return getattr(e, "code", None) == -32000 or any(w in text for w in (
        "auth", "login", "log in", "api key", "credential", "not configured", "unauthorized"))
