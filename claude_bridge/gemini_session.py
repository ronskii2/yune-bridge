"""Сессия Gemini CLI: ход — процесс `gemini -o stream-json` (следующие ходы — `--resume <id сессии>`), лента — те же
события, что у Claude Code и Codex, поэтому сайт рисует её тем же кодом. Очередь, «Стоп», модель — как у Codex
(codex_session.CodexSession): процесс живёт один ход, сообщение посреди хода ждёт следующего.

Gemini CLI сам держит историю сессии (~/.gemini/tmp/<проект>/chats), мост — только её id (столбец claude_id). Текст хода —
в stdin (stdin не терминал — Gemini CLI сам идёт без окна), картинки — ссылками @путь в тексте. Вход — из «Подключений»
(agents.Login: ссылка Google и код со страницы входа) или ключом GEMINI_API_KEY в окружении моста.

Доступ — режим одобрения Gemini CLI (--approval-mode), со следующего хода. Спрашивать без окна Gemini CLI не умеет: что
требует одобрения, в ходе без окна просто не выполняется.
  full → yolo (всё без вопросов)          auto → auto_edit (правит файлы сам, команды не запускает)
  ask  → default (только чтение и поиск)  read → plan (план: читает, ищет, правки — текстом)
MCP Yune Gemini CLI берёт только из файла своих настроек — в сессию Gemini его не подключаем (файл человека не трогаем).

События `gemini -o stream-json` → лента:
  init {session_id}                        → id сессии для --resume
  message {role: assistant, delta}         → копим, в ленту — целиком перед инструментом и в конце хода (text)
  tool_use {tool_name, tool_id, parameters} → tool (имена — как у Claude Code: Bash, Read, Edit, Write, Grep…)
  tool_result {tool_id, status, output}    → tool_result
  error {severity, message}                → error (лимит — limit, как у Claude и Codex) или note
  result {status, error?}                  → result (cost 0 — подписка Google или ключ)
"""
from __future__ import annotations

import asyncio
import time

from . import agents, codex_session
from .sessions import _trim, TRIM_RESULT, resets_from_text

APPROVAL = {"full": "yolo", "auto": "auto_edit", "ask": "default", "read": "plan"}
ACCESS_NOTE = {  # i18n: model
    "full": "[Режим доступа сессии: полный — правки и команды без вопросов.]",
    "auto": "[Режим доступа сессии: авто — файлы правишь сама, команды оболочки не запускаются.]",
    "ask": "[Режим доступа сессии: спрашивать — без окна спросить нельзя, поэтому только читай, ищи и предлагай правки "
           "текстом.]",
    "read": "[Режим доступа сессии: только чтение (план) — читай, ищи, объясняй и предлагай правки текстом.]",
}
# Инструменты Gemini CLI → имена ленты (сайт рисует их значками Claude Code); остальные — как есть
TOOL_NAMES = {"run_shell_command": "Bash", "read_file": "Read", "read_many_files": "Read", "write_file": "Write",
              "replace": "Edit", "edit": "Edit", "glob": "Glob", "search_file_content": "Grep", "grep": "Grep",
              "list_directory": "LS", "web_fetch": "WebFetch", "google_web_search": "WebSearch",
              "write_todos": "TodoWrite", "save_memory": "Memory"}
LIMIT_WORDS = ("quota", "resource_exhausted", "rate limit", "usage limit", "exhausted your")


class GeminiSession(codex_session.CodexSession):
    access_notes = ACCESS_NOTE
    agent_id = "gemini"
    has_effort = False                   # уровня размышлений у Gemini CLI нет: configure меняет только модель

    def __init__(self, bridge, row: dict):
        super().__init__(bridge, row)
        self.buf = ""

    async def _submit(self, text: str, images: list[str]) -> None:
        if not self.row["claude_id"]:
            text = codex_session.PREAMBLE + text
        self.queue.append((text, images))
        self._set_status("running")
        if not self.proc:
            asyncio.create_task(self._drain())

    def _argv(self, images: list[str]) -> list[str]:
        r = self.row
        argv = [agents.GEMINI_CLI, "-o", "stream-json", "--skip-trust", "--approval-mode", APPROVAL[self.access]]
        if r["model"] and r["model"] != "default":
            argv += ["-m", r["model"]]
        if r["claude_id"]:
            argv += ["--resume", r["claude_id"]]
        return argv

    def _input(self, text: str, images: list[str]) -> str:
        return text + ("\n\n" + "\n".join("@" + path for path in images) if images else "")

    async def _turn(self, text: str, images: list[str]) -> None:
        self.buf = ""
        await super()._turn(text, images)

    def _flush(self) -> None:
        if self.buf.strip():
            self._event({"kind": "text", "text": self.buf.strip()})
        self.buf = ""

    def _handle_codex(self, msg: dict) -> bool:
        """Событие gemini stream-json → события ленты. True — ход закончился (result записан)."""
        kind = msg.get("type")
        if kind == "init":
            sid = msg.get("session_id")
            if sid and sid != self.row["claude_id"]:
                self.bridge.update(self.id, claude_id=sid)
            self._set_status("running")
            return False
        if kind == "message":
            if msg.get("role") == "assistant":
                content = str(msg.get("content") or "")
                self.buf = self.buf + content if msg.get("delta") else content
            return False
        if kind == "tool_use":
            self._flush()
            params = msg.get("parameters") if isinstance(msg.get("parameters"), dict) else {}
            name = str(msg.get("tool_name") or "tool")
            inp = dict(params)
            path = params.get("file_path") or params.get("absolute_path") or params.get("path")
            if path and "file_path" not in inp:
                inp["file_path"] = path
            self._tool_once(str(msg.get("tool_id") or f"g{time.monotonic():.6f}"), TOOL_NAMES.get(name, name), inp)
            return False
        if kind == "tool_result":
            err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
            self._event({"kind": "tool_result", "tid": str(msg.get("tool_id") or ""), "error": msg.get("status") == "error",
                         "text": _trim(str(msg.get("output") or err.get("message") or ""), TRIM_RESULT)})
            return False
        if kind == "error":
            text = str(msg.get("message") or "")[:600]
            if msg.get("severity") == "warning" and not self._is_limit(text):
                self._event({"kind": "note", "text": text[:500]})
            else:
                self._codex_error(text)
            return False
        if kind == "result":
            self._flush()
            ms = int((time.monotonic() - self.started_at) * 1000)
            err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
            failed = msg.get("status") != "success"
            if failed and err.get("message"):
                self._codex_error(str(err["message"])[:600])
            self._event({"kind": "result", "cost": 0, "ms": ms, "turns": len(self.seen), "error": failed,
                         **({"errors": [str(err.get("message") or "")[:200]]} if failed else {})})
            # Доли окна нет: stats.input_tokens — сумма всех запросов хода, а не размер контекста
            return True
        return False

    @staticmethod
    def _is_limit(text: str) -> bool:
        low = text.lower()
        return any(w in low for w in LIMIT_WORDS)

    def _codex_error(self, text: str) -> None:
        """Ошибка Gemini CLI: квота («Quota exceeded», RESOURCE_EXHAUSTED) — событием limit, как у Claude и Codex."""
        if self._is_limit(text):
            if not self.limit_seen_turn:
                self.limit_seen_turn = True
                self.limit_reset = resets_from_text(text)
                self._event({"kind": "limit", "status": "rejected", "type": "gemini", "text": text,
                             "resets_at": self.limit_reset})
                if self.auto_resume:
                    self.schedule_resume()
            return
        if text and text != self.last_error:
            self.last_error = text
            self._event({"kind": "error", "code": "gemini", "text": text})
