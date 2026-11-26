"""Сессия Pi (pi.dev, npm @earendil-works/pi-coding-agent): `pi --mode rpc` — JSONL через stdin/stdout, не ACP.

Процесс живёт, пока сессия жива (гаснет по sweep, как Claude Code). Сессия Pi открывается по нашему id: `--session-id
<id сессии моста>` — Pi сам находит её файл в папке проекта или заводит новый, поэтому продолжение после рестарта —
тот же запуск. Команды: prompt (сообщение посреди хода — с streamingBehavior steer: Pi берёт его на следующем шаге, как
Claude Code), abort, set_model {provider, modelId}, get_available_models. Ход кончился — событие agent_settled.

Разрешения: Pi оставляет их расширениям. Мост подключает своё (PI_EXTENSION, `--extension`): перед любым инструментом,
кроме чтения, оно спрашивает клиента ctx.ui.confirm — мост отвечает extension_ui_response по режиму доступа сессии
(full — да, auto — правки да, остальное — карточкой, ask — карточкой, read — нет), поэтому режим меняется без
перезапуска Pi. Подсмотрено у T3 Code (provider-pi), 20.11.2026.

События → лента: message_update text_delta → delta, message_end (assistant) → text; tool_execution_start / _end → tool /
tool_result; ошибка провайдера (stopReason error) → error или limit; agent_settled → result.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time

from . import agents, codex_session, sessions
from .sessions import (ACCESS_NOTE, DATA, DELTA_EVERY_SEC, DELTA_FULL_SEC, DENY_SAID, BridgeError, Session, _trim,
                       TRIM_RESULT, resets_from_text)

TOOL_NAMES = {"bash": "Bash", "read": "Read", "edit": "Edit", "write": "Write", "grep": "Grep", "find": "Glob",
              "ls": "LS"}
EDIT_TOOLS = ("edit", "write")
LIMIT_WORDS = ("quota", "rate limit", "usage limit", "too many requests", "resource_exhausted", "429")
STOP_GRACE = 5
CONFIRM_PREFIX = "yune:"
# Расширение Pi: перед любым инструментом, кроме чтения, — вопрос клиенту (мосту); отказ — инструмент не запускается
PI_EXTENSION = """\
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
// Yune bridge: every non-read tool asks the client first (ctx.ui.confirm); the bridge answers by the session access mode
const READ_ONLY = new Set(["read", "grep", "find", "ls", "tool_search"]);
export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event: any, ctx: any) => {
    if (READ_ONLY.has(event.toolName)) return;
    const ok = await ctx.ui.confirm("yune:" + event.toolName, JSON.stringify(event.input ?? {}).slice(0, 4000),
                                    { signal: ctx.signal });
    if (!ok) return { block: true, reason: "Declined by the owner in Yune." };
  });
}
"""


def extension_path() -> str:
    path = os.path.join(DATA, "pi-yune-extension.ts")
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == PI_EXTENSION:
                return path
    except OSError:
        pass
    os.makedirs(DATA, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(PI_EXTENSION)
    return path


class PiSession(Session):
    steer = True
    access_notes = ACCESS_NOTE

    def __init__(self, bridge, row: dict):
        super().__init__(bridge, row)
        self.proc: asyncio.subprocess.Process | None = None
        self.waiters: dict[str, asyncio.Future] = {}
        self.next_id = 0
        self.turn = False
        self.turn_started = 0.0
        self.buf = ""
        self.seen: set[str] = set()
        self.last_error: str | None = None
        self.limit_seen_turn = False
        self.stderr_tail: list[str] = []
        self.settled: asyncio.Event | None = None

    @property
    def client(self):
        return self.proc if self.proc and self.proc.returncode is None else None

    @client.setter
    def client(self, value):
        pass

    # ── процесс и команды ────────────────────────────────────────────────────────────────────────────────────

    async def _ensure(self) -> None:
        if self.client:
            return
        sid = self.row["claude_id"] or self.id
        argv = [agents.CLIS["pi"], "--mode", "rpc", "--session-id", sid, "--extension", extension_path()]
        self.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.row["cwd"], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024, start_new_session=os.name != "nt")
        asyncio.create_task(self._read(self.proc))
        asyncio.create_task(self._stderr(self.proc))
        if sid != self.row["claude_id"]:
            self.bridge.update(self.id, claude_id=sid)
        found = await self._cmd("get_available_models", timeout=60)
        agents.remember_pi_models((found or {}).get("models") or [])
        model = self.row["model"]
        if model and model != "default" and "/" in model:
            provider, model_id = model.split("/", 1)
            try:
                await self._cmd("set_model", provider=provider, modelId=model_id, timeout=30)
            except BridgeError as e:
                self._event({"kind": "error", "code": "pi", "text": str(e)[:300]})

    def _write(self, obj: dict) -> None:
        if self.client:
            self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())

    async def _cmd(self, kind: str, timeout: float | None = None, **params) -> dict:
        self.next_id += 1
        rid = f"y{self.next_id}"
        fut = asyncio.get_running_loop().create_future()
        self.waiters[rid] = fut
        self._write({"id": rid, "type": kind, **params})
        await self.proc.stdin.drain()
        return await (asyncio.wait_for(fut, timeout) if timeout else fut)

    async def _read(self, proc) -> None:
        try:
            async for raw in proc.stdout:            # StreamReader режет только по \n — U+2028 внутри JSON не рвёт
                line = raw.decode(errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                try:
                    self._handle(msg)
                except Exception:  # noqa: BLE001
                    sessions.log.exception("[%s] pi event %s", self.id, msg.get("type"))
        finally:
            tail = "; ".join(self.stderr_tail[-3:])[:400]
            for fut in self.waiters.values():
                if not fut.done():
                    fut.set_exception(BridgeError("pi exited" + (f": {tail}" if tail else "")))
            self.waiters.clear()
            if self.turn:                            # процесс умер посреди хода
                self._error("pi exited" + (f": {tail}" if tail else ""))
                self._settle(error=True)

    async def _stderr(self, proc) -> None:
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                self.stderr_tail = (self.stderr_tail + [line])[-20:]

    # ── ход ──────────────────────────────────────────────────────────────────────────────────────────────────

    async def _submit(self, text: str, images: list[str]) -> None:
        if not self.row["claude_id"]:
            text = codex_session.PREAMBLE + text
        self._set_status("running")
        asyncio.create_task(self._send(text))

    async def _send(self, text: str) -> None:
        try:
            await self._ensure()
            self._live({"kind": "session", "session": self.public()})
            if self.turn:                            # посреди хода — Pi возьмёт на следующем шаге
                await self._cmd("prompt", message=text, streamingBehavior="steer", timeout=60)
                return
            self.turn, self.turn_started = True, time.monotonic()
            self.buf, self.seen, self.last_error, self.limit_seen_turn = "", set(), None, False
            self.live, self.live_at = "", 0
            res = await self._cmd("prompt", message=text, timeout=120)
            if (res or {}).get("disposition") == "handled":
                self._settle()
        except Exception as e:  # noqa: BLE001
            sessions.log.exception("[%s] pi send", self.id)
            self._error(str(e) or type(e).__name__, start=True)
            self._settle(error=True)

    def _settle(self, error: bool = False) -> None:
        if not self.turn:
            return
        self.turn = False
        self._flush_text()
        self._event({"kind": "result", "cost": 0, "ms": int((time.monotonic() - self.turn_started) * 1000),
                     "turns": len(self.seen), "error": error or self.turn_failed})
        self._set_status("idle")
        self._live({"kind": "session", "session": self.public()})
        asyncio.create_task(self._after_turn())

    def _error(self, text: str, start: bool = False) -> None:
        low = text.lower()
        if any(w in low for w in LIMIT_WORDS):
            if not self.limit_seen_turn:
                self.limit_seen_turn = True
                self.limit_reset = resets_from_text(text)
                self._event({"kind": "limit", "status": "rejected", "type": "pi", "text": text[:600],
                             "resets_at": self.limit_reset})
                if self.auto_resume:
                    self.schedule_resume()
            return
        if text != self.last_error:
            self.last_error = text
            code = "auth" if start and ("api key" in low or "login" in low) else "start" if start else "pi"
            if code == "auth":
                agents.note_login("pi", False)
            self._event({"kind": "error", "code": code, "text": text[:600]})

    def _flush_text(self) -> None:   # не _flush: то имя у Session — отправка сообщения агенту
        if self.buf.strip():
            self._event({"kind": "text", "text": self.buf.strip()})
        self.buf, self.live, self.live_at = "", "", 0

    def _delta(self, chunk: str) -> None:
        self.buf += chunk
        self.live += chunk
        now = time.monotonic()
        if now - self.live_sent >= DELTA_EVERY_SEC:
            self.live_sent = now
            if now - self.live_full >= DELTA_FULL_SEC:
                self.live_full = now
                self._live({"kind": "delta", "text": self.live})
            else:
                self._live({"kind": "delta", "add": self.live[self.live_at:], "at": self.live_at})
            self.live_at = len(self.live)

    def _handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "response":
            fut = self.waiters.pop(str(msg.get("id") or ""), None)
            if fut and not fut.done():
                if msg.get("success"):
                    fut.set_result(msg.get("data") or {})
                else:
                    fut.set_exception(BridgeError(str(msg.get("error") or "pi command failed")))
            return
        if kind == "extension_ui_request":
            asyncio.create_task(self._ui(msg))
            return
        self.last_active = time.monotonic()
        if kind == "message_update":
            ev = msg.get("assistantMessageEvent") or {}
            if ev.get("type") == "text_delta":
                self._delta(str(ev.get("delta") or ""))
        elif kind == "message_end":
            m = msg.get("message") or {}
            if m.get("role") == "assistant":
                self._flush_text()
                if m.get("stopReason") == "error" and m.get("errorMessage"):
                    self._error(str(m["errorMessage"]))
        elif kind == "tool_execution_start":
            self._flush_text()
            tid = str(msg.get("toolCallId") or "")
            if tid and tid not in self.seen:
                self.seen.add(tid)
                name = str(msg.get("toolName") or "tool")
                args = msg.get("args") if isinstance(msg.get("args"), dict) else {}
                inp = dict(args)
                if "path" in inp and "file_path" not in inp:
                    inp["file_path"] = inp["path"]
                self._event({"kind": "tool", "tid": tid, "name": TOOL_NAMES.get(name, name), "input": _trim(inp)})
        elif kind == "tool_execution_end":
            tid = str(msg.get("toolCallId") or "")
            result = msg.get("result")
            content = result.get("content") if isinstance(result, dict) else None
            text = "\n".join(str(c.get("text") or "") for c in content or [] if isinstance(c, dict)) \
                if content else (result if isinstance(result, str) else "")
            self._event({"kind": "tool_result", "tid": tid, "error": bool(msg.get("isError")),
                         "text": _trim(text, TRIM_RESULT)})
        elif kind == "agent_settled":
            self._settle()

    async def _ui(self, msg: dict) -> None:
        """Просьбы расширений Pi: confirm нашего расширения — разрешение по режиму доступа; остальные диалоги — отмена."""
        rid, method = str(msg.get("id") or ""), msg.get("method")
        if method != "confirm":
            if method in ("select", "input", "editor"):
                self._write({"type": "extension_ui_response", "id": rid, "cancelled": True})
            return
        title = str(msg.get("title") or "")
        tool = title[len(CONFIRM_PREFIX):] if title.startswith(CONFIRM_PREFIX) else ""
        ok = await self._allowed(tool, str(msg.get("message") or ""), rid) if tool else False
        self._write({"type": "extension_ui_response", "id": rid, "confirmed": bool(ok)})

    async def _allowed(self, tool: str, raw: str, rid: str) -> bool:
        access = self.access
        if access == "full" or (access == "auto" and tool in EDIT_TOOLS):
            return True
        if access == "read":
            return False
        try:
            inp = json.loads(raw) if raw else {}
        except ValueError:
            inp = {"input": raw}
        name = TOOL_NAMES.get(tool, tool)
        self._flush_text()
        self._event({"kind": "permission", "rid": rid, "name": name, "input": _trim(inp if isinstance(inp, dict) else {}, 1500),
                     "agent_id": None})
        answer = await self._wait(rid, "permission")
        if answer is None:
            return False
        decision, said = answer.get("decision"), answer.get("text") or None
        self._event({"kind": "permit", "rid": rid, "decision": decision, "text": said})
        if decision == "all":
            await self.set_access("full")
            return True
        if decision == "allow":
            return True
        if said and self.client:                     # слова владельца — агенту на следующем шаге
            self._write({"type": "prompt", "message": DENY_SAID.format(said), "streamingBehavior": "steer"})
        return False

    # ── управление ───────────────────────────────────────────────────────────────────────────────────────────

    async def stop(self) -> None:
        self.stopped = True
        self.cancel_resume()
        for _rid, (_kind, fut) in list(self.pending.items()):
            if not fut.done():
                fut.set_result(None)
        if not self.client or not self.turn:
            return
        self._write({"type": "abort"})
        self._event(dict(codex_session.STOP_NOTE))

    async def close(self) -> None:
        proc = self.proc
        self.proc = None
        if proc and proc.returncode is None:
            try:
                proc.stdin.close()
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
                await asyncio.wait_for(proc.wait(), STOP_GRACE)
            except (OSError, ProcessLookupError, asyncio.TimeoutError):
                try:
                    proc.kill()
                except (OSError, ProcessLookupError):
                    pass
        self.turn = False
        self._set_status("idle")

    async def _apply_access(self) -> None:
        """Режим доступа решает мост на каждом вопросе расширения — менять в процессе нечего."""

    async def configure(self, model: str | None, effort: str | None) -> None:
        if not model or model == self.row["model"]:
            return
        if not agents.valid_model("pi", model):
            raise BridgeError(codex_session.NO_MODEL)
        self.bridge.update(self.id, model=model)
        if self.client and "/" in model:
            provider, model_id = model.split("/", 1)
            await self._cmd("set_model", provider=provider, modelId=model_id, timeout=30)
        name = next((m["name"] for m in agents.models("pi") if m["id"] == model), model)
        self._event(codex_session.note_next(name))
