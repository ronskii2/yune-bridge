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


def models_of(result: dict) -> tuple[list[dict], str]:
    """Модели из ответа session/new | session/load: ACP v1 `models {availableModels, currentModelId}` или
    configOptions с category model. → ([{id, name, efforts}], текущая) — как дал агент."""
    out, current = [], ""
    models = result.get("models") if isinstance(result.get("models"), dict) else None
    if models:
        current = str(models.get("currentModelId") or "")
        for m in models.get("availableModels") or []:
            if isinstance(m, dict) and m.get("modelId"):
                out.append({"id": str(m["modelId"]), "name": str(m.get("name") or m["modelId"]), "efforts": []})
    for opt in result.get("configOptions") or []:
        if isinstance(opt, dict) and opt.get("category") == "model" and not out:
            current = str(opt.get("currentValue") or opt.get("value") or current)
            for v in opt.get("options") or []:
                if isinstance(v, dict) and (v.get("value") or v.get("id")):
                    vid = str(v.get("value") or v.get("id"))
                    out.append({"id": vid, "name": str(v.get("name") or vid), "efforts": []})
    return out, current


def model_option_id(result: dict) -> str | None:
    """id настройки модели в configOptions (для session/set_config_option), если агент даёт модели так."""
    for opt in result.get("configOptions") or []:
        if isinstance(opt, dict) and opt.get("category") == "model" and opt.get("id"):
            return str(opt["id"])
    return None


def is_auth_error(e: Exception) -> bool:
    text = str(getattr(e, "message", "") or e).lower()
    return getattr(e, "code", None) == -32000 or any(w in text for w in (
        "auth", "login", "log in", "api key", "credential", "not configured", "unauthorized"))
