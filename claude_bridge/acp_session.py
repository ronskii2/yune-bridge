"""Сессия агента по ACP (acp.py): Antigravity, Gemini CLI, OpenCode, Cursor, GitHub Copilot, Qwen Code, Grok, Kiro, goose — один код.

Процесс агента живёт, пока сессия жива (гаснет по sweep после IDLE_CLOSE_SEC тишины, как Claude Code); следующее
сообщение поднимает его снова и продолжает session/load по id (столбец claude_id), если агент умеет loadSession, —
иначе новая сессия агента (история — только в ленте Yune). Пока идёт session/load, агент заново присылает историю
уведомлениями — их не показываем.

Лента — те же события, что у Claude Code и Codex:
  agent_message_chunk → delta вживую, text целиком — перед инструментом и в конце хода; agent_thought_chunk — не идёт
  tool_call / tool_call_update → tool / tool_result (имена по kind: read → Read, edit → Edit, execute → Bash, …)
  plan → TodoWrite;  usage_update {used, size} → context;  ответ session/prompt (stopReason) → result

Доступ решает мост, а не агент: агент работает в своём обычном режиме и просит session/request_permission перед
правками и командами — ответ по режиму сессии: full — разрешить; auto — правки сами, команды (execute, delete) — карточкой
владельцу; ask — карточка на каждое изменяющее действие (как у Claude Code); read — отказ. Чтение и поиск — всегда сами.
Сообщение посреди хода — как у Codex: «Отправить сейчас» прерывает ход (session/cancel) и начинает новый.
MCP Yune — в session/new | session/load (mcpServers): напрямую по http, если агент умеет (mcpCapabilities.http),
иначе через stdio-прокси mcp_stdio.py --proxy (так Yune MCP работает в Antigravity и других stdio-only ACP-агентах).
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
import uuid

from . import acp, agents, codex_session, sessions
from .sessions import (ACCESS_NOTE, DELTA_EVERY_SEC, DELTA_FULL_SEC, DENY_SAID, Session, BridgeError, _trim,
                       TRIM_RESULT, resets_from_text)

# kind инструмента ACP → имя в ленте (сайт рисует значками Claude Code); think — мысли, в ленту не идут
KIND_NAMES = {"read": "Read", "edit": "Edit", "delete": "Delete", "move": "Edit", "search": "Grep", "execute": "Bash",
              "fetch": "WebFetch", "switch_mode": "ExitPlanMode"}
READ_KINDS = ("read", "search", "fetch", "think", "switch_mode")
RISKY_KINDS = ("execute", "delete", "other")        # «Авто»: правки сами, это — карточкой владельцу
LIMIT_WORDS = ("quota", "resource_exhausted", "rate limit", "usage limit", "exhausted your", "too many requests")
QUOTA_MSG_RE = re.compile(
    r"^(?:[\s\u26a0\ufe0f\u26d4\U0001f6ab\u2757\u274c\u2716\u23f1\u23f3!]*)(?:\[(?:!|warning|error)\]\s*)?(?:(?:warning|error)\s*:\s*)?"
    r"(?:individual\s+quota\s+reached|you(?:'ve| have)\s+(?:hit|reached)\s+your\s+.*limit|"
    r"quota\s+(?:exceeded|reached|exhausted)|rate\s+limit\s+(?:reached|exceeded)|resource_exhausted)\b",
    re.I,
)
CANCEL_WAIT = 15


def _error_text(e: Exception) -> str:
    """Полный текст ошибки ACP: JSON-RPC часто кладёт подробности (квота, Resets in …) в error.data."""
    msg = str(getattr(e, "message", None) or str(e) or type(e).__name__).strip()
    data = getattr(e, "data", None)
    detail = ""
    if isinstance(data, str):
        detail = data.strip()
    elif isinstance(data, dict):
        for k in ("details", "message", "error", "reason", "text"):
            if isinstance(data.get(k), str) and data[k].strip():
                detail = data[k].strip()
                break
        if not detail:
            detail = str(data).strip()
    if detail and detail.lower() not in msg.lower():
        return f"{msg}: {detail}" if msg and msg.lower() not in detail.lower() else detail
    return msg


class AcpSession(Session):
    steer = False                       # ACP не берёт сообщение посреди хода: «Отправить сейчас» — прервать и заново
    access_notes = ACCESS_NOTE

    def __init__(self, bridge, row: dict):
        super().__init__(bridge, row)
        self.agent = row.get("agent") or "gemini"
        self.proc: acp.AcpProc | None = None
        self.sid: str | None = None              # id сессии агента в этом процессе
        self.caps: dict = {}
        self.loading = False
        self.mcp_on = False
        self.model_option: str | None = None     # id настройки модели (configOptions), если агент даёт модели так
        self.effort_option: str | None = None    # id настройки мышления (configOptions thought_level), если есть
        self.queue: list[str] = []
        self.buf = ""
        self.seen: set[str] = set()
        self.turn_started = 0.0
        self.last_error: str | None = None
        self.limit_seen_turn = False
        self.said: list[str] = []                # что владелец написал, запретив действие, — агенту следующим ходом

    @property
    def client(self):                    # «живой процесс» для карточки и sweep
        return self.proc if self.proc and self.proc.alive else None

    @client.setter
    def client(self, value):
        pass

    # ── процесс и сессия агента ──────────────────────────────────────────────────────────────────────────────

    def _mcp_servers(self) -> list[dict]:
        servers: list[dict] = []
        if sessions.LOCAL_BRIDGE:
            env = [{"name": k, "value": v} for k, v in sessions.lozhka_mcp_env(self.id).items()]
            servers.append({"name": "lozhka", "command": sys.executable, "args": [sessions.lozhka_mcp_script()], "env": env})
        if sessions.YUNE_MCP:
            if (self.caps.get("mcpCapabilities") or {}).get("http"):
                servers.append({"type": "http", "name": "yune", "url": sessions.YUNE_MCP["url"],
                                "headers": [{"name": "Authorization", "value": f"Bearer {sessions.YUNE_MCP['token']}"},
                                            {"name": "X-Yune-Session", "value": self.id}]})
            else:
                env = [{"name": k, "value": v} for k, v in sessions.yune_mcp_stdio_env(self.id).items()]
                servers.append({"name": "yune", "command": sys.executable,
                                "args": [sessions.lozhka_mcp_script(), "--proxy"], "env": env})
        return servers

    async def _ensure(self) -> None:
        if self.client and self.sid:
            return
        await self._shutdown()
        self.proc = acp.AcpProc(agents.acp_argv(self.agent), self.row["cwd"], agents.acp_env(self.agent), self._notify,
                                self._request)
        await self.proc.start()
        init = await self.proc.call("initialize", acp.initialize_params(), 60)
        self.caps = init.get("agentCapabilities") or {}
        mcp = self._mcp_servers()
        result = None
        if self.row["claude_id"] and self.caps.get("loadSession"):
            self.loading = True
            try:
                result = await self.proc.call("session/load", {"sessionId": self.row["claude_id"], "cwd": self.row["cwd"],
                                                               "mcpServers": mcp}, 120)
                self.sid = self.row["claude_id"]
            except acp.AcpError as e:
                if acp.is_auth_error(e) and "session" not in e.message.lower():
                    raise
                sessions.log.info("[%s] acp load %s: %s — new session", self.id, self.agent, e.message)
            finally:
                self.loading = False
        if not self.sid:
            try:
                try:
                    result = await self.proc.call("session/new", {"cwd": self.row["cwd"], "mcpServers": mcp}, 120)
                except acp.AcpError as e:
                    method = agents.acp_auth_method(self.agent, init.get("authMethods") or [])
                    if not (acp.is_auth_error(e) and method):
                        raise
                    # вход есть (файл входа или ключ), но агент ждёт, что мы назовём способ, — authenticate и снова
                    await self.proc.call("authenticate", {"methodId": method}, 60)
                    result = await self.proc.call("session/new", {"cwd": self.row["cwd"], "mcpServers": mcp}, 120)
            except acp.AcpError as e:
                if acp.is_auth_error(e):
                    agents.note_login(self.agent, False)
                raise
            self.sid = str(result.get("sessionId") or "")
            if self.sid != self.row["claude_id"]:
                self.bridge.update(self.id, claude_id=self.sid)
        agents.note_login(self.agent, True)
        self.mcp_on = bool(mcp)
        found, current, cur_effort = acp.models_of(result or {})
        agents.remember_models(self.agent, found, current, cur_effort)
        self.model_option = acp.model_option_id(result or {})
        self.effort_option = acp.effort_option_id(result or {})
        model, effort = self.row["model"], self.row["effort"]
        if self.model_option:
            split = acp.split_legacy_model(model, found)
            if split:
                model, legacy_effort = split
                effort = effort or legacy_effort
                self.bridge.update(self.id, model=model, effort=effort)
        model_changed = False
        if model and model != "default" and model != current:
            await self._set_model(model)
            model_changed = True
        if effort and self.effort_option and (effort != cur_effort or model_changed):
            await self._set_effort(effort)

    async def _set_model(self, model: str) -> None:
        try:
            if self.model_option:
                res = await self.proc.call("session/set_config_option", {"sessionId": self.sid, "configId": self.model_option,
                                                                         "value": model}, 30)
                self.effort_option = acp.effort_option_id(res or {}) or self.effort_option
            else:
                await self.proc.call("session/set_model", {"sessionId": self.sid, "modelId": model}, 30)
        except acp.AcpError as e:
            sessions.log.info("[%s] acp set model %s: %s", self.id, model, e.message)
            self._event({"kind": "error", "code": "acp", "text": e.message[:300]})

    async def _set_effort(self, effort: str) -> None:
        if not self.effort_option:
            return
        try:
            await self.proc.call("session/set_config_option", {"sessionId": self.sid, "configId": self.effort_option,
                                                               "value": effort}, 30)
        except acp.AcpError as e:
            sessions.log.info("[%s] acp set effort %s: %s", self.id, effort, e.message)
            self._event({"kind": "error", "code": "acp", "text": e.message[:300]})

    async def _shutdown(self) -> None:
        if self.proc:
            await self.proc.close()
        self.proc, self.sid = None, None

    # ── ход ──────────────────────────────────────────────────────────────────────────────────────────────────

    async def _submit(self, text: str, images: list[str]) -> None:
        if not self.row["claude_id"]:
            extra = (sessions.SEND_FILE_NOTE + "\n" if sessions.LOCAL_BRIDGE else "") + (
                sessions.YUNE_NOTE + "\n" if sessions.YUNE_MCP else "")
            text = codex_session.PREAMBLE + extra + text
        self.queue.append(text)
        self._set_status("running")
        if not self.lock.locked():
            asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        async with self.lock:
            while self.queue:
                text = self.queue.pop(0)
                try:
                    await self._ensure()
                    self._live({"kind": "session", "session": self.public()})
                    await self._turn(text)
                    if self.said and not self.stopped:
                        # ACP не передаёт агенту слова к отказу — они уходят следующим сообщением
                        self.queue.append("\n".join(DENY_SAID.format(s) for s in self.said))
                    self.said = []
                except Exception as e:  # noqa: BLE001
                    sessions.log.exception("[%s] acp turn %s", self.id, self.agent)
                    self._error(_error_text(e), start=True)
                    self._event({"kind": "result", "cost": 0, "ms": 0, "turns": 0, "error": True})
                    if not (self.proc and self.proc.alive):
                        await self._shutdown()
            self._set_status("idle")
            self._live({"kind": "session", "session": self.public()})
        await self._after_turn()

    async def _turn(self, text: str) -> None:
        self.buf, self.seen, self.last_error, self.limit_seen_turn = "", set(), None, False
        self.live, self.live_at = "", 0
        self.turn_started = time.monotonic()
        self._set_status("running")
        res = await self.proc.call("session/prompt", {"sessionId": self.sid, "prompt": [{"type": "text", "text": text}]})
        stripped = self.buf.strip()
        quota_text = bool(stripped and len(stripped) <= 400 and "\n\n" not in stripped and QUOTA_MSG_RE.search(stripped))
        if quota_text:
            self.buf, self.live, self.live_at = "", "", 0
            self._error(stripped)
        else:
            self._flush_text()
        reason = str(res.get("stopReason") or "end_turn")
        failed = quota_text or reason in ("refusal", "error")
        if reason == "refusal" and not quota_text:
            self._error("refusal")
        self._event({"kind": "result", "cost": 0, "ms": int((time.monotonic() - self.turn_started) * 1000),
                     "turns": len(self.seen), "error": failed})

    def _error(self, text: str, start: bool = False) -> None:
        """Ошибка агента: квота — событием limit, как у Claude и Codex; не вошёл — error code auth; остальное — error."""
        low = text.lower()
        if any(w in low for w in LIMIT_WORDS):
            if not self.limit_seen_turn:
                self.limit_seen_turn = True
                self.limit_reset = resets_from_text(text)
                self._event({"kind": "limit", "status": "rejected", "type": self.agent, "text": text[:600],
                             "resets_at": self.limit_reset})
                if self.auto_resume:
                    self.schedule_resume()
            return
        if text != self.last_error:
            self.last_error = text
            code = "auth" if start and acp.is_auth_error(Exception(text)) else "start" if start else "acp"
            self._event({"kind": "error", "code": code, "text": text[:600]})

    def _flush_text(self) -> None:   # не _flush: то имя у Session — отправка сообщения агенту
        if self.buf.strip():
            self._event({"kind": "text", "text": self.buf.strip()})
        self.buf, self.live, self.live_at = "", "", 0

    # ── агент → мы ───────────────────────────────────────────────────────────────────────────────────────────

    def _notify(self, method: str, params: dict) -> None:
        if method != "session/update" or self.loading:
            return
        self.last_active = time.monotonic()
        u = params.get("update") or {}
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = u.get("content") or {}
            if content.get("type") == "text":
                self._delta(str(content.get("text") or ""))
        elif kind == "tool_call":
            self._flush_text()
            self._tool(u, new=True)
        elif kind == "tool_call_update":
            self._tool(u, new=False)
        elif kind == "plan":
            todos = [{"content": str(e.get("content") or ""), "status": e.get("status") or "pending"}
                     for e in u.get("entries") or [] if isinstance(e, dict)]
            tid = f"plan-{time.monotonic():.6f}"
            self.seen.add(tid)
            self._event({"kind": "tool", "tid": tid, "name": "TodoWrite", "input": _trim({"todos": todos})})
        elif kind == "config_option_update":
            opt = acp.effort_option_id(u)
            if opt:
                self.effort_option = opt
        elif kind == "usage_update":
            used, size = int(u.get("used") or 0), int(u.get("size") or 0)
            if used and size:
                percent = round(min(100.0, used * 100 / size), 1)
                self.bridge.update(self.id, context=percent, ctx_tokens=used)   # токены — для окошка сессии после перезагрузки
                self._live({"kind": "context", "percent": percent, "total": used, "max": size})

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

    @staticmethod
    def _is_send_file(u: dict) -> bool:
        title = str(u.get("title") or "").strip().lower()
        return bool(title and re.search(r"(?:^|[:/_.\s-])send_file$", title))

    @staticmethod
    def _name_input(u: dict) -> tuple[str, dict]:
        kind = str(u.get("kind") or "other")
        raw = u.get("rawInput") if isinstance(u.get("rawInput"), dict) else {}
        inp = dict(raw)
        if AcpSession._is_send_file(u):
            return "mcp__lozhka__send_file", inp
        locs = [loc.get("path") for loc in u.get("locations") or [] if isinstance(loc, dict) and loc.get("path")]
        if locs and "file_path" not in inp:
            inp["file_path"] = locs[0]
        name = KIND_NAMES.get(kind)
        if kind == "execute" and "command" not in inp and u.get("title"):
            inp["command"] = str(u["title"])
        if not name:
            name = str(u.get("title") or kind)[:80]
            if u.get("title") and "description" not in inp:
                inp["description"] = str(u["title"])[:300]
        return name, inp

    def _tool(self, u: dict, new: bool) -> None:
        tid = str(u.get("toolCallId") or "")
        if not tid or u.get("kind") == "think":
            return
        if tid not in self.seen and (new or u.get("kind") or u.get("title")):
            self.seen.add(tid)
            name, inp = self._name_input(u)
            self._event({"kind": "tool", "tid": tid, "name": name, "input": _trim(inp)})
        status = u.get("status")
        if status in ("completed", "failed") and tid in self.seen:
            parts = []
            for c in u.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "content" and isinstance(c.get("content"), dict):
                    parts.append(str(c["content"].get("text") or ""))
                elif c.get("type") == "diff":
                    parts.append(f"{c.get('path') or ''}: {len(str(c.get('newText') or '').splitlines())} lines")
            if not parts and isinstance(u.get("rawOutput"), (str, dict)):
                parts.append(str(u["rawOutput"]))
            self._event({"kind": "tool_result", "tid": tid, "error": status == "failed",
                         "text": _trim("\n".join(p for p in parts if p), TRIM_RESULT)})

    async def _request(self, method: str, params: dict) -> dict:
        if method == "session/request_permission":
            return await self._permission(params)
        raise acp.AcpError(-32601, f"method not supported: {method}")

    async def _permission(self, params: dict) -> dict:
        """Разрешение агенту — по режиму доступа сессии (шапка модуля): optionId из его вариантов allow_* / reject_*."""
        options = [o for o in params.get("options") or [] if isinstance(o, dict)]

        def pick(*kinds: str) -> dict:
            for k in kinds:
                opt = next((o for o in options if o.get("kind") == k), None)
                if opt:
                    return {"outcome": {"outcome": "selected", "optionId": opt.get("optionId")}}
            return {"outcome": {"outcome": "cancelled"}}

        call = params.get("toolCall") or {}
        kind = str(call.get("kind") or "other")
        access = self.access
        if kind in READ_KINDS or self._is_send_file(call) or access == "full" or (access == "auto" and kind not in RISKY_KINDS):
            return pick("allow_once", "allow_always")
        if access == "read":
            return pick("reject_once", "reject_always")
        name, inp = self._name_input(call)
        tid = str(call.get("toolCallId") or "")
        if tid and tid not in self.seen:
            self.seen.add(tid)
            self._flush_text()
            self._event({"kind": "tool", "tid": tid, "name": name, "input": _trim(inp)})
        rid = tid or uuid.uuid4().hex[:12]
        self._event({"kind": "permission", "rid": rid, "name": name, "input": _trim(inp, 1500), "agent_id": None})
        answer = await self._wait(rid, "permission")
        if answer is None:
            return {"outcome": {"outcome": "cancelled"}}
        decision, said = answer.get("decision"), answer.get("text") or None
        self._event({"kind": "permit", "rid": rid, "decision": decision, "text": said})
        if decision == "all":
            await self.set_access("full")
            return pick("allow_always", "allow_once")
        if decision == "allow":
            return pick("allow_once", "allow_always")
        if said:
            self.said.append(said)
        return pick("reject_once", "reject_always")

    # ── управление ───────────────────────────────────────────────────────────────────────────────────────────

    async def stop(self) -> None:
        self.stopped = True
        self.cancel_resume()
        self.queue.clear()
        for rid, (kind, fut) in list(self.pending.items()):
            if not fut.done():
                fut.set_result(None)
        if self.status == "idle" or not self.client:
            return
        self.proc.notify("session/cancel", {"sessionId": self.sid})
        self._event(dict(codex_session.STOP_NOTE))
        for _ in range(CANCEL_WAIT * 10):
            if not self.lock.locked():
                return
            await asyncio.sleep(0.1)
        await self._shutdown()                       # агент не остановился — гасим процесс, история — в load

    async def close(self) -> None:
        await self._shutdown()
        self._set_status("idle")

    async def _apply_access(self) -> None:
        """Режим доступа решает мост на каждой просьбе агента — менять в процессе нечего."""

    async def configure(self, model: str | None, effort: str | None) -> None:
        """Модель и уровень размышлений — сразу в живую сессию агента (или со следующего запуска)."""
        changes = {}
        if model and model != self.row["model"]:
            if not agents.valid_model(self.agent, model):
                raise BridgeError(codex_session.NO_MODEL)
            changes["model"] = model
        target = changes.get("model", self.row["model"])
        if effort and effort != self.row["effort"]:
            if not agents.valid_effort(self.agent, target, effort):
                raise BridgeError("no such level")
            changes["effort"] = effort
        elif self.row["effort"] and "model" in changes and not agents.valid_effort(self.agent, target, self.row["effort"]):
            changes["effort"] = ""
        if not changes:
            return
        self.bridge.update(self.id, **changes)
        if self.client and self.sid:
            if "model" in changes and self.row["model"] != "default":
                await self._set_model(self.row["model"])
            if self.row["effort"] and ("effort" in changes or "model" in changes):
                await self._set_effort(self.row["effort"])
        name = next((m["name"] for m in agents.models(self.agent) if m["id"] == self.row["model"]), self.row["model"])
        what = ", ".join(x for x in (name, self.row["effort"]) if x)
        self._event(codex_session.note_next(what))
