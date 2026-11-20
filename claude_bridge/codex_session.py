"""Сессия Codex: ход — процесс `codex exec --json` (следующие ходы — `codex exec resume <thread>`), лента — те же
события, что у Claude Code (sessions.py), поэтому сайт рисует её тем же кодом.

Codex сам держит историю треда (~/.codex/sessions), мост — только id треда (столбец claude_id: у Claude это id
сессии для resume, у Codex — id треда). Процесс живёт один ход: ответ готов — процесс вышел. Сообщение во время
хода ждёт в очереди моста (sessions.py, outbox) и уходит следующим ходом; «отправить сейчас» прерывает ход — посреди
хода Codex сообщений не берёт. «Стоп» — SIGINT процессу (Codex сохраняет тред), через 5 с — kill. «You've hit your
usage limit» — событие limit, как у Claude (время обновления Codex пишет по-разному — автопродолжение раз в полчаса).

Codex в режиме exec не спрашивает вопросов и ключей: вопросы — обычным текстом в ответе, ключи владелец кладёт сам.
Доступ — режим сессии (sessions.ACCESS, столбец access), со следующего хода: full (по умолчанию) — как у Claude Code в
мосте, без песочницы и подтверждений (--dangerously-bypass-approvals-and-sandbox); ask — песочница workspace-write
(правки только в папке проекта, без сети): подтверждать по одному exec не умеет, сайт так и подписывает; read —
песочница read-only. Песочница — через -c sandbox_mode: у `exec resume` нет флага --sandbox.

События `codex exec --json` → лента:
  item.completed agent_message     → text            reasoning          → в ленту не идёт (мысли — внутри агента)
  item.started   command_execution → tool Bash       item.completed     → tool_result (вывод, код выхода)
  item.completed file_change       → tool Edit/Write/Delete на каждый файл
  mcp_tool_call → tool mcp__<сервер>__<тул> + tool_result;  web_search → tool WebSearch;  todo_list → tool TodoWrite
  turn.completed → result (cost 0 — подписка; context — доля окна модели);  turn.failed / error → error + result
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time

from . import agents, sessions
from .sessions import BridgeError, Session, _trim, TRIM_RESULT, resets_from_text

log = logging.getLogger("claude_bridge")

PREAMBLE = """[Сессия идёт с сайта владельца (раздел «Подключения»): он видит твои ответы и шаги в ленте, часто говорит
голосом — текст распознан автоматически, возможны ошибки распознавания, понимай по смыслу. Ключи и пароли не проси
прислать текстом — скажи, в какой env-файл и под каким именем их положить. Файлы, которые прислал владелец, лежат по
путям из сообщения.]

"""
STOP_GRACE = 5
# Режим доступа → флаги хода. approval_policy never: в exec подтверждать некому — что песочница не пускает, просто не выходит
ACCESS_ARGS = {
    "full": ["--dangerously-bypass-approvals-and-sandbox"],
    "ask": ["-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"',
            "-c", "sandbox_workspace_write.network_access=false"],
    "read": ["-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"'],
    # auto — как codex --full-auto: песочница проекта, сеть открыта (подтверждать в exec некому)
    "auto": ["-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"',
             "-c", "sandbox_workspace_write.network_access=true"],
}
ACCESS_NOTE = {  # i18n: model
    "full": "[Режим доступа сессии: полный — без песочницы.]",
    "ask": "[Режим доступа сессии: песочница — правки только в папке проекта, сеть закрыта.]",
    "read": "[Режим доступа сессии: только чтение — песочница не даст менять файлы; читай, ищи, объясняй и предлагай "
            "правки текстом.]",
    "auto": "[Режим доступа сессии: авто — правки только в папке проекта, сеть открыта.]",
}
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


class CodexSession(Session):
    steer = False
    agent_id = "codex"
    has_effort = True                    # уровень размышлений — параметр хода (у Gemini CLI его нет)
    access_notes = ACCESS_NOTE

    def __init__(self, bridge, row: dict):
        super().__init__(bridge, row)
        self.proc: asyncio.subprocess.Process | None = None
        self.queue: list[tuple[str, list[str]]] = []
        self.started_at = 0.0
        self.seen: set[str] = set()         # item.id, у которых уже есть событие tool (started пришёл раньше completed)
        self.failed = False
        self.stderr_tail: list[str] = []
        self.last_error: str | None = None
        self.limit_seen_turn = False

    @property
    def client(self):                    # «живой процесс» для карточки и sweep: у Codex — идущий ход
        return self.proc

    @client.setter
    def client(self, value):
        pass

    # ── ход ──────────────────────────────────────────────────────────────────────────────────────────────────

    async def _submit(self, text: str, images: list[str]) -> None:
        if not self.row["claude_id"]:
            text = PREAMBLE + (sessions.YUNE_NOTE + "\n" if sessions.YUNE_MCP else "") + text
        self.queue.append((text, images))
        self._set_status("running")
        if not self.proc:
            asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        async with self.lock:
            while self.queue:
                text, images = self.queue.pop(0)
                try:
                    await self._turn(text, images)
                except Exception as e:  # noqa: BLE001
                    log.exception("[%s] codex turn", self.id)
                    self._event({"kind": "error", "code": "start", "text": str(e)[:500]})
                    self._event({"kind": "result", "cost": 0, "ms": 0, "turns": 0, "error": True})
            self._set_status("idle")
            self._live({"kind": "session", "session": self.public()})
        await self._after_turn()

    def _argv(self, images: list[str]) -> list[str]:
        r = self.row
        argv = [agents.CODEX_CLI, "exec"]
        if r["claude_id"]:
            argv.append("resume")
        for path in images:
            argv += ["-i", path]
        argv += ["--json", *ACCESS_ARGS[self.access], "--skip-git-repo-check"]
        if r["model"] and r["model"] != "default":
            argv += ["-m", r["model"]]
        argv += ["-c", f'model_reasoning_effort="{r["effort"]}"']
        if sessions.YUNE_MCP:          # MCP Yune по адресу сайта: ключ машины — из окружения, не в командной строке
            argv += ["-c", f'mcp_servers.yune.url="{sessions.YUNE_MCP["url"]}"',
                     "-c", 'mcp_servers.yune.bearer_token_env_var="YUNE_MCP_TOKEN"']
        if r["claude_id"]:
            argv.append(r["claude_id"])
        else:
            argv += ["-C", r["cwd"]]
        argv.append("-")                                  # текст хода — из stdin: без ограничений длины командной строки
        return argv

    async def _turn(self, text: str, images: list[str]) -> None:
        self.failed, self.seen, self.stderr_tail = False, set(), []
        self.last_error, self.limit_seen_turn = None, False
        self.started_at = time.monotonic()
        self.proc = await asyncio.create_subprocess_exec(
            *self._argv(images), cwd=self.row["cwd"], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024, start_new_session=os.name != "nt",
            env={**os.environ, "YUNE_MCP_TOKEN": sessions.YUNE_MCP["token"]} if sessions.YUNE_MCP else None)
        self._live({"kind": "session", "session": self.public()})
        self.proc.stdin.write(self._input(text, images).encode())
        await self.proc.stdin.drain()
        self.proc.stdin.close()
        err_task = asyncio.create_task(self._stderr(self.proc))
        done = False
        try:
            async for raw in self.proc.stdout:
                self.last_active = time.monotonic()
                line = raw.decode(errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                try:
                    done = self._handle_codex(msg) or done
                except Exception:  # noqa: BLE001
                    log.exception("[%s] codex event %s", self.id, msg.get("type"))
        finally:
            code = await self.proc.wait()
            await err_task
            self.proc = None
        if not done:
            stopped = code in (-signal.SIGINT, -signal.SIGTERM, -signal.SIGKILL, 130)
            if not stopped:
                tail = "; ".join(self.stderr_tail[-3:])[:400]
                self._event({"kind": "error", "code": "process", "text": f"exit {code}" + (f": {tail}" if tail else "")})
            self._event({"kind": "result", "cost": 0, "ms": int((time.monotonic() - self.started_at) * 1000),
                         "turns": 0, "error": not stopped})

    def _input(self, text: str, images: list[str]) -> str:
        """Текст хода в stdin (картинки Codex берёт флагами -i, gemini_session — ссылками @путь в тексте)."""
        return text

    async def _stderr(self, proc) -> None:
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                self.stderr_tail = (self.stderr_tail + [line])[-20:]
                log.debug("[%s] codex: %s", self.id, line[:300])

    def _handle_codex(self, msg: dict) -> bool:
        """Событие codex exec → события ленты. True — ход закончился (result записан)."""
        kind = msg.get("type")
        if kind == "thread.started":
            tid = msg.get("thread_id")
            if tid and tid != self.row["claude_id"]:
                self.bridge.update(self.id, claude_id=tid)
            return False
        if kind == "turn.started":
            self._set_status("running")
            return False
        if kind in ("item.started", "item.updated", "item.completed"):
            self._item(msg.get("item") or {}, kind.split(".")[1])
            return False
        if kind == "turn.completed":
            usage = msg.get("usage") or {}
            ms = int((time.monotonic() - self.started_at) * 1000)
            self._event({"kind": "result", "cost": 0, "ms": ms, "turns": len(self.seen), "error": False})
            used = int(usage.get("input_tokens") or 0)
            if used:
                window = agents.codex_context_window()
                percent = round(min(100.0, used * 100 / window), 1)
                self.bridge.update(self.id, context=percent)
                self._live({"kind": "context", "percent": percent, "total": used, "max": window})
            return True
        if kind == "turn.failed":
            text = ((msg.get("error") or {}).get("message") or "turn failed")[:600]
            self._codex_error(text)
            self._event({"kind": "result", "cost": 0, "ms": int((time.monotonic() - self.started_at) * 1000),
                         "turns": len(self.seen), "error": True, "errors": [text[:200]]})
            return True
        if kind == "error":
            self._codex_error(str(msg.get("message") or "")[:600])
        return False

    def _codex_error(self, text: str) -> None:
        """Ошибка Codex: лимит подписки («You've hit your usage limit») — событием limit, как у Claude; одно и то же
        приходит и error, и turn.failed — в ленту один раз."""
        if "usage limit" in text.lower() or "rate limit" in text.lower():
            if not self.limit_seen_turn:
                self.limit_seen_turn = True
                self.limit_reset = resets_from_text(text)
                self._event({"kind": "limit", "status": "rejected", "type": "codex", "text": text,
                             "resets_at": self.limit_reset})
                if self.auto_resume:
                    self.schedule_resume()
            return
        if text != self.last_error:
            self.last_error = text
            self._event({"kind": "error", "code": "codex", "text": text})

    def _item(self, item: dict, phase: str) -> None:
        typ, iid = item.get("type"), str(item.get("id") or "")
        if typ == "agent_message" and phase == "completed":
            if (item.get("text") or "").strip():
                self._event({"kind": "text", "text": item["text"]})
        elif typ == "command_execution":
            self._tool_once(iid, "Bash", {"command": item.get("command") or ""})
            if phase == "completed":
                code = item.get("exit_code")
                self._event({"kind": "tool_result", "tid": iid, "error": item.get("status") == "failed" or
                             (code not in (None, 0)), "text": _trim(item.get("aggregated_output") or "", TRIM_RESULT)})
        elif typ == "file_change" and phase == "completed":
            for n, change in enumerate(item.get("changes") or []):
                name = {"add": "Write", "delete": "Delete"}.get(change.get("kind"), "Edit")
                tid = f"{iid}-{n}"
                self._tool_once(tid, name, {"file_path": change.get("path") or ""})
                self._event({"kind": "tool_result", "tid": tid, "error": item.get("status") == "failed", "text": ""})
        elif typ == "mcp_tool_call":
            name = f"mcp__{item.get('server') or 'mcp'}__{item.get('tool') or 'tool'}"
            self._tool_once(iid, name, item.get("arguments") if isinstance(item.get("arguments"), dict) else {})
            if phase == "completed":
                result = item.get("result") or {}
                content = result.get("content") if isinstance(result, dict) else None
                text = "\n".join(c.get("text") or "" for c in content or [] if isinstance(c, dict))
                err = item.get("error") or {}
                self._event({"kind": "tool_result", "tid": iid, "error": item.get("status") == "failed",
                             "text": _trim(text or (err.get("message") if isinstance(err, dict) else "") or "", TRIM_RESULT)})
        elif typ == "web_search":
            self._tool_once(iid, "WebSearch", {"query": item.get("query") or ""})
        elif typ == "todo_list" and phase != "started":
            self.seen.discard(iid)          # план меняется по ходу — каждый раз новой строкой с текущим списком
            self._tool_once(iid + f"-{time.monotonic():.3f}", "TodoWrite",
                            {"todos": [{"content": t.get("text") or "", "status": "completed" if t.get("completed")
                                        else "pending"} for t in item.get("items") or []]})
        elif typ == "error":
            self._event({"kind": "note", "text": (item.get("message") or "Ошибка")[:500]})

    def _tool_once(self, tid: str, name: str, inp: dict) -> None:
        if tid in self.seen:
            return
        self.seen.add(tid)
        self._event({"kind": "tool", "tid": tid, "name": name, "input": _trim(inp)})

    # ── управление ───────────────────────────────────────────────────────────────────────────────────────────

    async def stop(self) -> None:
        self.stopped = True
        self.cancel_resume()
        await self._kill()

    async def _kill(self) -> None:
        self.queue.clear()
        proc = self.proc
        if not proc:
            return
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGINT)
        except (OSError, ProcessLookupError):
            return
        self._event({"kind": "note", "text": "Остановлено"})
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

    async def close(self) -> None:
        await self._kill()
        self._set_status("idle")

    async def _apply_access(self) -> None:
        """Песочница — параметр запуска: новый режим — со следующего хода (идущий доделывает в прежнем)."""

    async def configure(self, model: str | None, effort: str | None) -> None:
        """Модель и уровень — параметры запуска: действуют со следующего хода."""
        changes = {}
        if model and model != self.row["model"]:
            if not agents.valid_model(self.agent_id, model):
                raise BridgeError("нет такой модели")
            changes["model"] = model
        if effort and self.has_effort and effort != self.row["effort"]:
            if effort not in agents.EFFORTS:
                raise BridgeError("нет такого уровня")
            changes["effort"] = effort
        if not changes:
            return
        self.bridge.update(self.id, **changes)
        name = next((m["name"] for m in agents.models(self.agent_id) if m["id"] == self.row["model"]), self.row["model"])
        if self.has_effort:
            name += ", " + str(self.row["effort"])
        self._event({"kind": "note", "text": f"Дальше: {name}"})
