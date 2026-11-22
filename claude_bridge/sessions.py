"""Сессии Claude Code с сайта (Yune, «Подключения»): живой процесс `claude` на сессию через Claude Agent SDK.

Процесс — тот же Claude Code, что в терминале (`~/.local/bin/claude`, вход по подписке, настройки и скиллы
пользователя): по умолчанию (режим доступа full, bypassPermissions) Claude решает всё сам; «Разрешить?» — только в
режиме ask (ниже). Наружу — ровная лента событий:

  user        сообщение владельца                      text        реплика или ответ Claude (готовый блок)
  tool        вызов тула: tid, name, input (обрезан)
  tool_result итог тула: tid, error, text (обрезан)    question    AskUserQuestion: rid, questions
  answer      ответ владельца на вопрос: rid, answers  secret      запрос ключа (тул расширения): rid, name, purpose, …
  secret_done ключ записан / отказ: rid, ok, …         result      ход закончен: cost, ms, turns, error
  note        служебное («контекст сжат», «остановлено»)  error   ошибка процесса/модели
  file        Claude показал файл (mcp__lozhka__send_file): fid, name, mime, size, caption
  permission  агент просит разрешения (режим «Спрашивать»): rid, name, input (обрезан)
  permit      ответ владельца на просьбу: rid, decision allow | deny | all, text — что сказал агенту вместо
  limit       лимит подписки: status warning (used — доля, resets_at) | rejected (ход упал, text — как написал Claude
              Code) | overage; type — five_hour | seven_day | …
  (у user — files: [{fid, name, mime, size}] — что владелец приложил; Claude получает их пути строкой; auto: resume —
  «продолжай» после лимита или ошибки, текст рисует сайт; у error — code: rate_limit, server_error, … и text как есть)

Мышление в ленту не идёт (владелец, 15.11.2026: мысли — внутри агента, на сайте — только то, что он говорит): Claude
Code получает его скрытым (thinking display omitted) — сводку мышления ни API, ни мост не гоняют.

Только вживую (в базу не пишутся): delta — текст ответа по мере набора: add — дописанное с позиции at, раз в
DELTA_FULL_SEC — text целиком (подключились посреди ответа — догоняют по нему); status — idle/running/waiting,
context — заполненность контекста, session — карточка сессии изменилась.

Режим доступа сессии (столбец access, меняется в любой момент, сайт — значком под полем; владелец 18.11.2026):
  full — по умолчанию, как было: bypassPermissions, без вопросов;
  ask  — правки и команды — с разрешения владельца: permission_mode default, хук PreToolUse отдаёт изменяющие инструменты
         в can_use_tool (решение «ask» — даже если их разрешают allow-правила настроек), там — карточка на сайте и ожидание,
         как у AskUserQuestion; чтение и поиск — без вопросов; «Разрешить всё в этой сессии» — режим full;
  read — только чтение: хук сразу отказывает изменяющим инструментам (и тем, что разрешены allow-правилами).
Claude Code меняет режим сразу (set_permission_mode живому процессу; хук и can_use_tool читают режим на каждом вызове),
Codex — со следующего хода (codex_session: песочница workspace-write / read-only вместо --dangerously-bypass…).

Права агента (rules.py, столбец rules; сессия, которую сайт открыл от агента Yune): какие файлы читать и писать, какие
команды сам / с вопросом / нельзя. Сайт задаёт их при создании, сменить их нельзя (новые права — новая сессия); хук
решает каждое действие (отказ — заметкой code rule_denied в ленте), режим SDK у такой сессии — default. Режим доступа
действует поверх: «Спрашивать» и «Только чтение» строже прав, «Полный» их не отменяет. Только Claude Code (у Codex хука нет).

Вопрос с вариантами (AskUserQuestion) приходит в can_use_tool даже в bypassPermissions (проверено 04.11.2026) —
ход ждёт ответа владельца с сайта. Местное расширение (extension.py) может добавить свои тулы — например, запрос
ключа в закрытое поле на сайте (события secret / secret_done, ответ — POST /sessions/{id}/secret).

Отложенные сообщения (outbox): написанное, пока агент работает, ждёт конца хода — его можно изменить, отменить или
отправить сразу (Claude Code берёт такое сообщение в идущий ход на следующем шаге, Codex — прерывает ход). Ход
закончился без ошибки — очередь уходит одним сообщением; после «Стоп», ошибки или лимита ждёт владельца.
Лимит подписки: «Продолжить» — сразу, «Продолжить, когда восстановится» — столбец resume_at, срабатывает sweep;
снова упёрлись в лимит — ждём следующего окна сами.

Процесс гаснет после IDLE_CLOSE_SEC тишины; следующее сообщение поднимает его снова с resume (история Claude —
в ~/.claude/projects). Пока у процесса Claude есть фоновые команды (Bash run_in_background: сборка, brew upgrade) — не
гасим: они его дети и умерли бы вместе с ним (19.11.2026: долгий brew upgrade убит молча через 10 минут после хода);
дольше BG_MAX_SEC — гасим и пишем в ленту, что фоновые команды остановлены. Рестарт моста живые ходы обрывает — сессии остаются, продолжаются тем же resume.

Агент сессии — столбец agent: claude (здесь), codex (codex_session.CodexSession) или агент по ACP — Gemini CLI,
OpenCode, Cursor и другие (agents.ACP, acp_session.AcpSession) — та же лента событий. «Продолжить
в другом агенте» (handoff) — новая сессия в том же проекте с выжимкой прежней: сообщения владельца, итоговые ответы,
изменённые файлы; выжимка уходит агенту вместе с первым сообщением (столбец intro), в ленте — событие handoff.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import time
import uuid
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, PermissionResultAllow,
                              PermissionResultDeny, create_sdk_mcp_server, tool)
from claude_agent_sdk.types import (AssistantMessage, ResultMessage, StreamEvent, SystemMessage, TextBlock,
                                    ToolResultBlock, ToolUseBlock, UserMessage)
try:                                 # мост на ПК может стоять со старым SDK — без событий лимита
    from claude_agent_sdk.types import RateLimitEvent
except ImportError:  # pragma: no cover
    RateLimitEvent = None

from . import agents, extension, projects
from . import rules as agent_rules

warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")

log = logging.getLogger("claude_bridge")

DATA = os.environ.get("CLAUDE_BRIDGE_DATA") or os.path.expanduser("~/.yune-bridge")
CLI = agents.CLAUDE_CLI
# Процесс claude — ~300 МБ; кеш промпта у API живёт 5 минут, держать процесс дольше — только память (15.11.2026)
IDLE_CLOSE_SEC = 10 * 60
BG_MAX_SEC = 6 * 3600
# Автосжатие (переключатель «auto_compact», сайт — в окне лимитов): за столько до конца часового кэша подписки простаивающая
# сессия с контекстом от AUTO_COMPACT_MIN_TOKENS сама делает /compact (порог — как у строки «Сжать» на сайте)
AUTO_COMPACT_LEAD_SEC = 5 * 60
AUTO_COMPACT_MIN_TOKENS = 50000
DELTA_EVERY_SEC = 0.12
DELTA_FULL_SEC = 3
TRIM = 4000
TRIM_RESULT = 6000
FILES_DIR = os.path.join(DATA, "files")           # файлы сессии: присланные владельцем и показанные Claude
MAX_FILE = 50 * 1024 * 1024

MODELS = agents.CLAUDE_MODELS
EFFORTS = agents.EFFORTS
DEFAULT_MODEL, DEFAULT_EFFORT = agents.DEFAULTS["claude"]
EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit", "Delete")
# Режим доступа сессии: full — без вопросов (по умолчанию), ask — изменяющее с разрешения владельца, read — только чтение
# auto (владелец 18.11.2026) — режим auto Claude Code: безопасное он выполняет сам, сомнительное — спросит (can_use_tool → карточка)
ACCESS = ("full", "auto", "ask", "read")
SDK_MODE = {"full": "bypassPermissions", "auto": "auto", "ask": "default", "read": "default"}
# Чтение, поиск и служебное — без вопросов в любом режиме. Остальное (Bash, Edit, Write, MultiEdit, NotebookEdit,
# неизвестные и сторонние инструменты) — изменяющее: в «Спрашивать» — карточка, в «Только чтение» — отказ
READ_TOOLS = frozenset({
    "Read", "Glob", "Grep", "LS", "NotebookRead", "WebSearch", "WebFetch", "TodoWrite", "Task", "Agent", "ToolSearch",
    "BashOutput", "TaskOutput", "KillShell", "KillBash", "ListMcpResourcesTool", "ReadMcpResourceTool", "Skill",
    "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
    "mcp__lozhka__send_file",                                   # показать файл; тулы расширения — extension READ_TOOLS
    "mcp__yune__agent_propose",       # предложить агента: только предложение, включает пользователь (и в «Только чтение»)
})
# Инструменты MCP по глаголу в имени: get_issue, list_pages, image_list, job_status, fetch_url — чтение
READ_VERBS = frozenset({"get", "list", "read", "search", "find", "fetch", "view", "describe", "status", "guide",
                        "lookup"})


def reads(name: str) -> bool:
    """Инструмент только читает: без вопросов и в «Только чтение»."""
    if name in READ_TOOLS or name in (extension.attr("READ_TOOLS") or ()):
        return True
    if name.startswith("mcp__") and name.count("__") >= 2:
        words = [w for w in re.split(r"[_\-]+", name.split("__", 2)[2].lower()) if w]
        return bool(words) and (words[0] in READ_VERBS or words[-1] in READ_VERBS)
    return False


ACCESS_NOTE = {  # i18n: model
    "full": "[Режим доступа сессии: полный — правки и команды без подтверждений.]",
    "ask": "[Режим доступа сессии: «Спрашивать» — перед правкой файлов и командами владелец подтверждает каждое действие "
           "на сайте; запретил — не повторяй то же действие в обход, спроси или предложи другое.]",
    "read": "[Режим доступа сессии: только чтение — правки файлов и команды запрещены; читай, ищи, объясняй и предлагай "
            "правки текстом.]",
    "auto": "[Режим доступа сессии: авто — обычные правки и команды выполняются сами, рискованные Claude Code сначала "
            "проверяет; если спросит владельца — запретил, не повторяй то же действие в обход.]",
}
DENY_READ = ("Режим сессии «Только чтение»: правки файлов и команды запрещены владельцем. Не пытайся обойти — "  # i18n: model
             "предложи правку текстом.")
DENY_ASK = "Владелец запретил это действие."  # i18n: model
DENY_SAID = "Владелец запретил это действие и написал: {}"  # i18n: model
RESUME_GAP = 60                     # автопродолжение — через минуту после обновления лимита
RETRY_SEC = 30 * 60                 # когда обновится, неизвестно (Codex, текст без времени) — пробуем раз в полчаса
RESUME = "Лимит восстановился — продолжай с того места, где остановился."  # i18n: model
RETRY = "Продолжай с того места, где остановился."  # i18n: model
RESETS_RE = re.compile(r"resets\s+(?:(?P<mon>[A-Za-z]{3})[a-z]*\s+(?P<day>\d{1,2}),?\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?"
                       r"\s*(?P<ap>am|pm)?\s*(?:\((?P<tz>[^)]+)\))?", re.I)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

APPEND = """
Сессия идёт с сайта владельца: он видит твои ответы и шаги в ленте, часто говорит голосом — текст распознан
автоматически, возможны ошибки распознавания — понимай по смыслу, переспрашивай, только если смысл правда неясен.
Вопрос с вариантами — только тулом AskUserQuestion: на сайте это карточка с кнопками.
{keys}Значение ключа не выводи (не cat/echo/grep по файлу с ним): нужен в команде — подставляй из файла (source, $VAR).
Показать владельцу файл (скриншот сделанного, картинку, PDF, архив, лог) — тулом mcp__lozhka__send_file: картинки и
видео он увидит прямо в ленте, остальное — файлом для скачивания. Скриншот страницы — Playwright с хоста.
Файлы, которые прислал владелец, лежат по путям из сообщения — открывай их Read.
Отвечай на языке, на котором пишет владелец (пишет по-русски — отвечай по-русски), даже если настройки или правила
машины велят другой язык.
"""

# Мост на машине пользователя (remote.py): MCP Yune на сайте — {"url": site + "/mcp", "token": "m.<машина>.<ключ машины>"}.
# Инструменты Yune (картинки, видео, музыка, голос…) идут через сайт (его MCP) — обновление доходит до машин
# с выкаткой сайта, мост трогать не нужно. Мост по сокету (server.py) — None.
YUNE_MCP: dict | None = None
YUNE_NOTE = ("Инструменты Yune (картинки, видео, музыка, голос, чтение ссылок, презентации, 3D) — mcp__yune__*: тратят "  # i18n: model
             "баланс владельца в Yune (у машины дневной лимит), файлы приходят ссылками — скачивай curl. Платное — "
             "только когда владелец просит.\n")


# ключи — как просить без расширения; расширение со своим тулом ключей отдаёт свой текст (keys_prompt)
KEYS = ("Ключи, токены, пароли никогда не проси прислать текстом: скажи, в какой env-файл и под каким именем их "  # i18n: model
        "положить.\n")


def append_prompt() -> str:  # i18n: model
    keys = extension.attr("keys_prompt")
    text = APPEND.replace("{keys}", (keys() if keys else "") or KEYS)
    return text + YUNE_NOTE if YUNE_MCP else text


def yune_mcp_server(session_id: str) -> dict:
    """Сервер MCP Yune для Claude Code: ключ машины и id сессии — сайт по нему знает, какие проекты пользователь
    открыл агенту в этой сессии (розетка «Проекты»: «нельзя» — не видны)."""
    return {"type": "http", "url": YUNE_MCP["url"],
            "headers": {"Authorization": f"Bearer {YUNE_MCP['token']}", "X-Yune-Session": session_id}}


# Устройство, с которого владелец начал сессию (сайт берёт из User-Agent): строкой перед первым
# сообщением (столбец intro) — один раз, в системную подсказку не идёт, кэш подсказки у всех сессий общий
DEVICE = "[Владелец начал сессию с устройства: {}. Видео ему — mp4 (H.264), webm Safari не играет.]\n\n"  # i18n: model


def device_note(device) -> str | None:
    device = re.sub(r"[^\w .·()/-]", "", str(device or ""))[:60].strip()
    return DEVICE.format(device) if device else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', project TEXT NOT NULL, project_name TEXT NOT NULL,
    cwd TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL, claude_id TEXT, created REAL NOT NULL,
    updated REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0, context REAL, cost REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session_idx ON events (session, id);
CREATE TABLE IF NOT EXISTS outbox (
    qid TEXT PRIMARY KEY, session TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_session_idx ON outbox (session, ts);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
# Столбцы, добавленные позже (SQLite: ADD COLUMN без IF NOT EXISTS — проверяем сами)
COLUMNS = {"agent": "TEXT NOT NULL DEFAULT 'claude'", "intro": "TEXT", "origin": "TEXT", "resume_at": "REAL",
           "access": "TEXT NOT NULL DEFAULT 'full'", "rules": "TEXT",
           # кэш контекста у Anthropic (сайт предлагает сжать, когда он остывает): время последнего запроса хода, срок
           # жизни (подписка — час, ключ API и докупка сверх лимита — 5 минут) и размер контекста в токенах
           "cache_at": "REAL", "cache_ttl": "INTEGER", "ctx_tokens": "INTEGER"}


class BridgeError(Exception):
    pass


def _cache_fields(usage: dict | None, row: dict) -> dict:
    """Ход прочёл или записал кэш — кэш живёт заново с конца хода; срок — по тому, куда Claude Code писал (1h / 5m).
    Ход без запросов к API (ошибка до запроса) кэш не трогает."""
    usage = usage or {}
    made = usage.get("cache_creation") or {}
    if not (usage.get("cache_read_input_tokens") or usage.get("cache_creation_input_tokens")):
        return {}
    ttl = 3600 if made.get("ephemeral_1h_input_tokens") else 300 if made.get("ephemeral_5m_input_tokens") else row.get("cache_ttl")
    return {"cache_at": time.time(), "cache_ttl": ttl}


def _trim(value, limit: int = TRIM):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"\n…[ещё {len(value) - limit} зн.]"
    if isinstance(value, dict):
        return {k: _trim(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim(v, limit) for v in value[:200]]
    return value


def resets_from_text(text: str, now: float | None = None) -> float | None:
    """«You've hit your 5-hour limit · resets 8:40pm (Asia/Tokyo)» / «resets Nov 14, 2pm (Asia/Tokyo)» → unix-время."""
    m = RESETS_RE.search(text or "")
    if not m:
        return None
    try:
        tz = ZoneInfo(m["tz"].strip()) if m["tz"] else datetime.now().astimezone().tzinfo
    except (KeyError, ValueError):
        tz = datetime.now().astimezone().tzinfo
    hour = int(m["h"]) % 12 + (12 if (m["ap"] or "").lower() == "pm" else 0) if m["ap"] else int(m["h"])
    if hour > 23:
        return None
    base = datetime.fromtimestamp(now or time.time(), tz)
    at = base.replace(hour=hour, minute=int(m["m"] or 0), second=0, microsecond=0)
    if m["mon"] and m["mon"][:3].lower() in MONTHS:
        try:
            at = at.replace(month=MONTHS.index(m["mon"][:3].lower()) + 1, day=int(m["day"]))
        except ValueError:
            return None
        if at < base - timedelta(days=1):
            at = at.replace(year=at.year + 1)
    elif at < base:
        at += timedelta(days=1)
    return at.timestamp()


def _result_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(item.get("text") or "")
            elif item.get("type") == "image":
                parts.append("[картинка]")
    return "\n".join(parts)


class Session:
    def __init__(self, bridge: "Bridge", row: dict):
        self.bridge = bridge
        self.row = row
        self.id = row["id"]
        self.client: ClaudeSDKClient | None = None
        self.reader: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.status = "idle"
        self.pending: dict[str, tuple[str, asyncio.Future]] = {}
        self.last_active = time.monotonic()
        self.live = ""
        self.live_sent = 0.0
        self.live_at = 0                    # сколько текста ответа уже ушло сайту (delta add)
        self.live_full = 0.0
        self.outbox: list[dict] = bridge.outbox(self.id)    # отложенные сообщения: ждут конца хода
        self.stopped = False                # «Стоп» — очередь ждёт владельца, а не уходит сама
        self.turn_failed = False            # ход упал (ошибка, лимит) — очередь тоже ждёт
        self.compacted = False              # в ходе было сжатие контекста (compact_boundary)
        self.auto_resume = False            # ход запущен автопродолжением: снова лимит — ждём следующего окна
        self.limit_reset: float | None = None
        self.access_told: str | None = None     # режим доступа, о котором агент уже знает в этом процессе моста

    steer = True                            # сообщение посреди хода агент берёт на следующем шаге (у Codex — нет)
    access_notes = ACCESS_NOTE              # о режиме доступа — агенту (у Codex — про песочницу)
    access_modes = ACCESS                   # какие режимы показать на сайте (старые мосты поля не шлют — три прежних)

    # ── карточка и события ──────────────────────────────────────────────────

    def public(self) -> dict:
        r = self.row
        # пустое название — пустым: подпись «Новая сессия» ставит сайт на языке витрины
        return {"id": r["id"], "title": r["title"] or "", "project": r["project"],
                "project_name": r["project_name"], "cwd": r["cwd"], "model": r["model"], "effort": r["effort"],
                "agent": r.get("agent") or "claude", "origin": r.get("origin"),
                "created": r["created"], "updated": r["updated"], "context": r["context"], "cost": r["cost"],
                "cache_at": r.get("cache_at"), "cache_ttl": r.get("cache_ttl"), "ctx_tokens": r.get("ctx_tokens"),
                # автосжатие моста включено — сайт не зовёт «Сжать» руками
                "auto_compact": self.bridge.auto_compact if (r.get("agent") or "claude") == "claude" else None,
                "status": self.status, "live": bool(self.client),
                "pending": [{"rid": rid, "kind": kind} for rid, (kind, _f) in self.pending.items()],
                "queue": [{"qid": q["qid"], "ts": q["ts"], "text": q["text"], "files": q.get("files_info") or [],
                           "parts": [t for t in q.get("parts") or [] if t] or None, "hold": bool(q.get("hold"))} for q in self.outbox],
                "resume_at": r.get("resume_at"), "steer": self.steer,
                # режим доступа: поле есть — мост умеет режимы (сайт показывает значок под полем; старые мосты — без него)
                "access": self.access, "access_modes": list(self.access_modes),
                # права агента (rules.py): есть — сессия идёт от агента Yune, мост проверяет каждое действие
                "rules": self.rules}

    @property
    def rules(self) -> dict | None:
        raw = self.row.get("rules")
        if not raw:
            return None
        try:
            return agent_rules.clean(json.loads(raw))
        except ValueError:
            return None

    def _sdk_mode(self) -> str:
        """Режим SDK: у сессии с правами агента — default (каждое действие решает хук), иначе — по режиму доступа."""
        return "default" if self.rules else SDK_MODE[self.access]

    @property
    def access(self) -> str:
        return self.row.get("access") if self.row.get("access") in ACCESS else "full"

    def _event(self, data: dict) -> None:
        if data.get("kind") == "error" or (data.get("kind") == "limit" and data.get("status") == "rejected"):
            self.turn_failed = True
        self.bridge.add_event(self.id, data)

    def _live(self, data: dict) -> None:
        self.bridge.publish({"session": self.id, **data})

    def _set_status(self, status: str) -> None:
        if status == "running" and self.pending:
            status = "waiting"
        if status != self.status:
            self.status = status
            self._live({"kind": "status", "status": status})

    # ── процесс claude ──────────────────────────────────────────────────────

    def _host_tools(self) -> list:
        tools = extension.attr("tools")
        return list(tools(self)) if tools else []

    def _file_tool(self):
        schema = {"type": "object",
                  "properties": {"path": {"type": "string", "description": "Абсолютный путь к файлу на сервере"},
                                 "caption": {"type": "string", "description": "Подпись для владельца (по-русски), можно пусто"}},
                  "required": ["path"]}

        @tool("send_file", "Показать владельцу файл с сервера в ленте сессии на сайте: картинки и видео — прямо в "
              "ленте, остальное — файлом для скачивания. До 50 МБ. Файл копируется, оригинал можно менять дальше.", schema)
        async def send_file(args):
            path = os.path.expanduser(str(args.get("path") or ""))
            if not os.path.isabs(path):
                path = os.path.join(self.row["cwd"], path)
            try:
                info = await asyncio.to_thread(self.bridge.store_file, self.id, path)
            except (OSError, BridgeError) as e:
                return {"content": [{"type": "text", "text": f"Файл не отправлен: {e}"}], "is_error": True}
            info.pop("path", None)
            self._event({"kind": "file", **info, "caption": str(args.get("caption") or "")[:1000]})
            return {"content": [{"type": "text", "text": f"Владелец видит {info['name']} в ленте."}]}

        return send_file

    def _options(self) -> ClaudeAgentOptions:
        r = self.row
        return ClaudeAgentOptions(
            cli_path=CLI, cwd=r["cwd"], model=MODELS.get(r["model"], MODELS[DEFAULT_MODEL])[1], effort=r["effort"],
            permission_mode=self._sdk_mode(), can_use_tool=self._can_use_tool,
            # режим доступа — на каждом вызове инструмента (меняется посреди сессии без перезапуска процесса)
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self._pre_tool])]},
            # тулы местного расширения (extension.py) — рядом с «показать файл»
            # раскладку по соседним сессиям (open_sessions) владелец отключил 16.11.2026: по замеру почти не окупается
            mcp_servers={"lozhka": create_sdk_mcp_server("lozhka", tools=self._host_tools()
                                                         + [self._file_tool()]),
                         **({"yune": yune_mcp_server(self.id)} if YUNE_MCP else {})},
            include_partial_messages=True, setting_sources=["user", "project", "local"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": append_prompt()},
            # мышление — скрытым: на сайт его не шлём, сводку (по-английски) незачем и получать (15.11.2026)
            thinking={"type": "adaptive", "display": "omitted"},
            # строка потока больше 1 МБ (снимок экрана в ответе инструмента) роняла чтение по умолчанию (14.11.2026)
            max_buffer_size=64 * 1024 * 1024,
            # Claude в Chrome (переключатель машины в окне лимитов, владелец 20.11.2026): без флага сессия Chrome не видит,
            # даже когда расширение стоит; действует с нового процесса
            extra_args={"chrome": None} if self.bridge.chrome else {},
            resume=r["claude_id"] or None, stderr=lambda line: log.debug("[%s] %s", self.id, line))

    async def _ensure(self) -> None:
        async with self.lock:
            if self.client:
                return
            client = ClaudeSDKClient(self._options())
            await client.connect()
            self.client = client
            self.reader = asyncio.create_task(self._read(client))
            self._live({"kind": "session", "session": self.public()})

    async def close(self) -> None:
        async with self.lock:
            client, self.client = self.client, None
            for _kind, fut in self.pending.values():
                if not fut.done():
                    fut.set_result(None)
            if client:
                try:
                    await client.disconnect()
                except Exception as e:  # noqa: BLE001
                    log.info("[%s] disconnect: %s", self.id, e)
            if self.reader:
                self.reader.cancel()
                self.reader = None
        self._set_status("idle")
        self._live({"kind": "session", "session": self.public()})

    async def _read(self, client: ClaudeSDKClient) -> None:
        try:
            async for msg in client.receive_messages():
                self.last_active = time.monotonic()
                try:
                    self._handle(msg)
                except Exception:  # noqa: BLE001
                    log.exception("[%s] handle %s", self.id, type(msg).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] reader: %s", self.id, e)
            self._event({"kind": "error", "code": "process", "text": str(e)[:500]})
        if self.client is not client:       # закрыл close() — процесс гасит он; второй disconnect падал на None.returncode
            return
        self.client = None
        self.reader = None
        self._set_status("idle")
        self._live({"kind": "session", "session": self.public()})
        # Чтение кончилось — процесс claude гасим: иначе он жил дальше без нас, доделывал ход втёмную (сообщение,
        # отправленное в идущий ход, — тоже), а следующее сообщение поднимало второй процесс на ту же беседу (14.11.2026)
        try:
            await client.disconnect()
        except Exception as e:  # noqa: BLE001
            log.info("[%s] disconnect: %s", self.id, e)

    def _handle(self, msg) -> None:
        if isinstance(msg, StreamEvent):
            if msg.parent_tool_use_id:
                return
            ev = msg.event or {}
            kind = ev.get("type")
            if kind == "message_start":
                self._set_status("running")
            elif kind == "content_block_start" and (ev.get("content_block") or {}).get("type") == "text":
                self.live, self.live_at = "", 0
            elif kind == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                self.live += ev["delta"].get("text") or ""
                now = time.monotonic()
                if now - self.live_sent >= DELTA_EVERY_SEC:
                    self.live_sent = now
                    # дописанное, а не весь текст каждые 0,12 с; целиком — изредка, для тех, кто открыл посреди ответа
                    if now - self.live_full >= DELTA_FULL_SEC:
                        self.live_full = now
                        self._live({"kind": "delta", "text": self.live})
                    else:
                        self._live({"kind": "delta", "add": self.live[self.live_at:], "at": self.live_at})
                    self.live_at = len(self.live)
            return
        if RateLimitEvent is not None and isinstance(msg, RateLimitEvent):
            self._rate_limit(msg.rate_limit_info)
            return
        if isinstance(msg, AssistantMessage) and msg.error and not msg.parent_tool_use_id:
            # Ошибка API — текстом от Claude Code («You've hit your 5-hour limit · resets 8:40pm»): не ответом, а
            # событием с кодом — сайт рисует карточку с кнопками
            text = " ".join(b.text for b in msg.content if isinstance(b, TextBlock)).strip()
            self._api_error(str(msg.error), text)
            return
        if isinstance(msg, AssistantMessage):
            self._set_status("running")
            parent = msg.parent_tool_use_id
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    self._event({"kind": "text", "text": block.text, "parent": parent})
                    if not parent:
                        self.live = ""
                elif isinstance(block, ToolUseBlock):
                    self._event({"kind": "tool", "tid": block.id, "name": block.name, "input": _trim(block.input),
                                 "parent": parent})
            if msg.error:
                self._event({"kind": "error", "code": str(msg.error), "text": ""})
            return
        if isinstance(msg, UserMessage):
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        self._event({"kind": "tool_result", "tid": block.tool_use_id, "error": bool(block.is_error),
                                     "text": _trim(_result_text(block.content), TRIM_RESULT),
                                     "parent": msg.parent_tool_use_id})
            return
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                claude_id = (msg.data or {}).get("session_id")
                if claude_id and claude_id != self.row["claude_id"]:
                    self.bridge.update(self.id, claude_id=claude_id)
            elif msg.subtype == "compact_boundary":
                self.compacted = True
                self._event({"kind": "note", "code": "compacted", "text": "Контекст сжат"})   # text — старым сайтам
            return
        if isinstance(msg, ResultMessage):
            self.live = ""
            if msg.session_id and msg.session_id != self.row["claude_id"]:
                self.bridge.update(self.id, claude_id=msg.session_id)
            cost = msg.total_cost_usd or 0.0
            # ход из одного /compact (0 шагов): Claude Code ещё отдаёт размер и кэш прежнего контекста — сайт снова звал
            # «Сжать 66k», автосжатие ждало старый срок. Сжатого ещё нет ни в кэше, ни в замере — до следующего хода пусто.
            # Сжатие посреди хода (контекст кончился) — после него были запросы, замер верный
            compacted = self.compacted and not msg.num_turns
            self.compacted = False
            cache = {"cache_at": None, "context": None, "ctx_tokens": None} if compacted else _cache_fields(msg.usage, self.row)
            self.bridge.update(self.id, cost=(self.row["cost"] or 0) + cost, **cache)
            reason = getattr(msg, "terminal_reason", None) or ""
            self._event({"kind": "result", "cost": cost, "ms": msg.duration_ms, "turns": msg.num_turns,
                         "error": bool(msg.is_error), "errors": (msg.errors or [])[:5],
                         "status": getattr(msg, "api_error_status", None),
                         "stopped": reason.startswith("aborted") or None})
            self._set_status("idle")
            if compacted:
                self._live({"kind": "context", "percent": None, "total": None})
            else:
                asyncio.create_task(self._context())
            asyncio.create_task(self._after_turn())

    # ── лимит подписки и ошибки API ────────────────────────────────────────

    def _rate_limit(self, info) -> None:
        """Состояние лимита от Claude Code: в мост (общий на все сессии — лимит у аккаунта), предупреждение — в ленту
        один раз на окно лимита."""
        kind = info.rate_limit_type or "five_hour"
        state = {"status": info.status, "type": kind, "resets_at": info.resets_at, "used": info.utilization,
                 "overage": info.overage_status}
        self.bridge.limit_state(kind, state)
        if info.status == "rejected" and info.overage_status == "allowed":
            status = "overage"                  # подписка кончилась, а работа идёт за доплату
        elif info.status == "allowed_warning":
            status = "warning"
        else:
            return
        if self.bridge.limit_once((kind, status, info.resets_at)):
            self._event({"kind": "limit", "status": status, "type": kind, "resets_at": info.resets_at,
                         "used": info.utilization})

    def _api_error(self, code: str, text: str) -> None:
        if code != "rate_limit":
            self._event({"kind": "error", "code": code, "text": text[:2000]})
            return
        known = self.bridge.limit_rejected()
        resets = (known or {}).get("resets_at") or resets_from_text(text)
        kind = (known or {}).get("type") or ("seven_day" if "weekly" in text.lower() else "five_hour")
        self.limit_reset = resets
        self._event({"kind": "limit", "status": "rejected", "type": kind, "resets_at": resets, "text": text[:600]})
        if self.auto_resume:                # автопродолжение снова упёрлось в лимит — ждём следующего окна
            self.schedule_resume()

    def schedule_resume(self, at: float | None = None) -> None:
        """«Продолжить, когда восстановится»: время — из лимита (+ минута), иначе — попытка через полчаса."""
        now = time.time()
        at = at or ((self.limit_reset + RESUME_GAP) if self.limit_reset and self.limit_reset > now else now + RETRY_SEC)
        self.bridge.update(self.id, resume_at=max(at, now + 5))

    def cancel_resume(self) -> None:
        self.auto_resume = False
        if self.row.get("resume_at"):
            self.bridge.update(self.id, resume_at=None)

    async def resume(self, auto: bool = False) -> None:
        """Продолжить после лимита или ошибки: отложенные сообщения — вместе с «продолжай»."""
        if self.row.get("resume_at"):
            self.bridge.update(self.id, resume_at=None)
        if self.status != "idle":
            return
        self.auto_resume = auto
        lead = RESUME if self.limit_reset else RETRY
        await self._flush([q for q in self.outbox if not q.get("hold")], lead=lead)

    async def _after_turn(self) -> None:
        """Ход закончился: без ошибки и «Стоп» — отложенные сообщения уходят одним следующим сообщением."""
        failed, self.turn_failed = self.turn_failed, False
        if failed or self.stopped:
            return
        self.auto_resume = False
        self.limit_reset = None
        ready = [q for q in self.outbox if not q.get("hold")]
        if ready and self.status == "idle":
            await self._flush(ready)

    async def _context(self) -> None:
        client = self.client
        if not client:
            return
        try:
            usage = await asyncio.wait_for(client.get_context_usage(), 20)
        except Exception as e:  # noqa: BLE001
            log.info("[%s] context usage: %s", self.id, e)
            return
        percent = round(float(usage.get("percentage") or 0), 1)
        self.bridge.update(self.id, context=percent, ctx_tokens=int(usage.get("totalTokens") or 0) or None)
        self._live({"kind": "context", "percent": percent, "total": usage.get("totalTokens"),
                    "max": usage.get("maxTokens")})

    # ── вопросы и ключи: ход ждёт владельца ─────────────────────────────────

    async def _wait(self, rid: str, kind: str):
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = (kind, fut)
        self._set_status("waiting")
        self._live({"kind": "session", "session": self.public()})
        try:
            return await fut
        finally:
            self.pending.pop(rid, None)
            self._set_status("running" if self.client else "idle")
            self._live({"kind": "session", "session": self.public()})

    async def _pre_tool(self, data: dict, tool_use_id, ctx) -> dict:
        """Хук PreToolUse: решение по режиму доступа раньше allow-правил настроек и режима SDK. full и чтение — без
        мнения ({}); read — отказ сразу; ask — «спросить»: вызов уходит в can_use_tool, там ждём владельца."""
        name = str((data or {}).get("tool_name") or "")
        access = self.access
        rules = self.rules
        if rules and name != "AskUserQuestion":
            # права агента: нельзя — отказ сразу; спросить — карточка; можно — дальше решает режим доступа сессии
            verdict, why = self._rule(rules, name, (data or {}).get("tool_input") or {})
            if verdict == "deny":
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": why}}
            if verdict == "ask" or (access == "ask" and not reads(name)):
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask"}}
            if access == "read" and not reads(name):
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": DENY_READ}}
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
        if access in ("full", "auto") or reads(name):
            return {}                           # auto — решает сам Claude Code; спросит — придёт в can_use_tool
        if access == "read":
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": DENY_READ}}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask"}}

    def _rule(self, rules: dict, name: str, inp: dict) -> tuple[str, str]:
        """Решение по правам агента; отказ — заметкой в ленту (сайт собирает из них журнал прав агента)."""
        verdict, why = agent_rules.decide(rules, name, inp, self.row["cwd"], reads(name))
        if verdict == "deny":
            what = inp.get("command") or inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or name
            self._event({"kind": "note", "code": "rule_denied", "name": name, "what": str(what)[:200], "text": why})
        return verdict, why

    async def _can_use_tool(self, name: str, inp: dict, ctx):
        rules = self.rules
        if rules and name != "AskUserQuestion":
            verdict, why = agent_rules.decide(rules, name, inp, self.row["cwd"], reads(name))
            if verdict == "deny":
                return PermissionResultDeny(message=why)
            if verdict == "ask" or (self.access == "ask" and not reads(name)):
                return await self._permit(name, inp, ctx)
            if self.access == "read" and not reads(name):
                return PermissionResultDeny(message=DENY_READ)
            return PermissionResultAllow(updated_input=inp)
        if name != "AskUserQuestion":
            access = self.access
            if access == "full" or reads(name):
                return PermissionResultAllow(updated_input=inp)
            if access == "read":
                return PermissionResultDeny(message=DENY_READ)
            return await self._permit(name, inp, ctx)
        rid = ctx.tool_use_id or uuid.uuid4().hex[:12]
        self._event({"kind": "question", "rid": rid, "questions": _trim(inp.get("questions") or [])})
        answers = await self._wait(rid, "question")
        if answers is None:
            return PermissionResultDeny(message="Владелец остановил ход", interrupt=True)
        self._event({"kind": "answer", "rid": rid, "answers": answers})
        return PermissionResultAllow(updated_input={**inp, "answers": answers})

    async def _permit(self, name: str, inp: dict, ctx):
        """«Спрашивать»: карточка на сайте (событие permission, пуш), ход ждёт ответа владельца. Запретил с текстом —
        агент получает текст вместо действия; «Разрешить всё в этой сессии» — режим full."""
        rid = getattr(ctx, "tool_use_id", None) or uuid.uuid4().hex[:12]
        self._event({"kind": "permission", "rid": rid, "name": name, "input": _trim(inp, 1500),
                     "agent_id": getattr(ctx, "agent_id", None)})
        answer = await self._wait(rid, "permission")
        if answer is None:
            return PermissionResultDeny(message="Владелец остановил ход", interrupt=True)  # i18n: model
        decision, said = answer.get("decision"), answer.get("text") or None
        self._event({"kind": "permit", "rid": rid, "decision": decision, "text": said})
        if decision == "all":
            await self.set_access("full")
        if decision in ("allow", "all"):
            return PermissionResultAllow(updated_input=inp)
        if self.access == "read":                # пока ждали, владелец включил «Только чтение»
            return PermissionResultDeny(message=DENY_READ)
        return PermissionResultDeny(message=DENY_SAID.format(said) if said else DENY_ASK)

    async def set_access(self, access: str) -> None:
        """Режим доступа сессии. Claude Code — сразу (живому процессу set_permission_mode), Codex — со следующего хода.
        Ждущие разрешения решаются новым режимом: «Полный доступ» — разрешить, «Только чтение» — запретить."""
        if access not in ACCESS:
            raise BridgeError("нет такого режима")  # i18n: internal
        if access == self.access:
            return
        self.bridge.update(self.id, access=access)
        await self._apply_access()
        if access in ("full", "read"):
            verdict = {"decision": "allow" if access == "full" else "deny"}
            for rid, (kind, fut) in list(self.pending.items()):
                if kind == "permission" and not fut.done():
                    fut.set_result(verdict)
        self._event({"kind": "note", "access": access, "agent": self.row.get("agent") or "claude"})

    async def _apply_access(self) -> None:
        client = self.client
        if not client:
            return                          # следующий процесс поднимется с новым режимом (_options)
        try:
            await client.set_permission_mode(self._sdk_mode())
        except Exception as e:  # noqa: BLE001
            # старый CLI не умеет менять режим на ходу — хук и can_use_tool всё равно смотрят на новый режим; режим
            # SDK (bypass → default) — с перезапуском процесса, когда ход закончится
            log.info("[%s] set_permission_mode: %s", self.id, e)
            if self.status == "idle":
                await self.close()

    def _access_note(self) -> str:
        """Режим доступа — агенту в конце сообщения, когда поменялся (в ленту не попадает). Новая сессия в полном
        доступе — ничего; после рестарта моста про режим «Спрашивать» / «Только чтение» напоминаем снова."""
        access, told = self.access, self.access_told
        self.access_told = access
        if access == told or (told is None and access == "full"):
            return ""
        return "\n\n" + self.access_notes[access]

    def resolve(self, rid: str, kind: str, value) -> None:
        entry = self.pending.get(rid)
        if not entry or entry[0] != kind:
            raise BridgeError("этот вопрос уже закрыт")
        if not entry[1].done():
            entry[1].set_result(value)

    # ── действия владельца ──────────────────────────────────────────────────

    async def send(self, text: str, fids: list[str] | None = None, answer: dict | None = None,
                   mode: str = "auto") -> None:
        """Сообщение владельца. Агент работает — в очередь (mode later; now — сразу в идущий ход), свободен — сразу,
        вместе с тем, что ждало в очереди."""
        text = text.strip()
        files = [f for f in (self.bridge.file_info(self.id, fid) for fid in (fids or [])[:20]) if f]
        if not text and not files:
            raise BridgeError("пустое сообщение")
        self.last_active = time.monotonic()
        if not self.row["title"]:
            title = " ".join(text.split())[:70] or files[0]["name"]
            self.bridge.update(self.id, title=title)
        item = {"qid": uuid.uuid4().hex[:12], "ts": time.time(), "text": text, "files": [f["fid"] for f in files],
                "files_info": [{k: f[k] for k in ("fid", "name", "mime", "size")} for f in files], "answer": answer}
        if mode != "now" and (self.status != "idle" or mode == "later"):
            # Отложенные подряд — одно сообщение: второе почти всегда дополнение к первому (владелец, 14.11.2026).
            # Дописываем в последнее ждущее, если его сейчас не правят
            last = self.outbox[-1] if self.outbox and not self.outbox[-1].get("hold") else None
            if last:
                last["parts"] = (last.get("parts") or [last["text"]]) + [text]
                last["text"] = "\n\n".join(t for t in last["parts"] if t)
                last["files"] = (last.get("files") or []) + item["files"]
                last["files_info"] = (last.get("files_info") or []) + item["files_info"]
                last["answer"] = answer or last.get("answer")
                item = last
            else:
                self.outbox.append(item)
            self.bridge.outbox_put(self.id, item)
            self._live({"kind": "session", "session": self.public()})
            return
        ready = [q for q in self.outbox if not q.get("hold")] if self.status == "idle" else []
        await self._flush(ready + [item])

    async def queue_action(self, qid: str, op: str, text: str | None = None) -> None:
        """Отложенное сообщение: edit (новый текст), hold (владелец правит — не отправлять), cancel, now."""
        item = next((q for q in self.outbox if q["qid"] == qid), None)
        if not item:
            raise BridgeError("сообщение уже отправлено")
        if op == "edit":
            text = (text or "").strip()
            if not text and not item.get("files"):
                raise BridgeError("пустое сообщение")
            item.update(text=text, hold=False, parts=None)
            self.bridge.outbox_put(self.id, item)
        elif op == "hold":
            item["hold"] = True
            self.bridge.outbox_put(self.id, item)
        elif op == "cancel":
            self._unqueue([item])
        elif op == "now":
            if self.status != "idle" and not self.steer:
                await self.stop()               # Codex не берёт сообщение посреди хода — прерываем ход
                self.stopped = False
            await self._flush([item])
            return
        else:
            raise BridgeError("unknown op")
        self._live({"kind": "session", "session": self.public()})

    def _unqueue(self, items: list[dict]) -> None:
        gone = {q["qid"] for q in items}
        self.outbox = [q for q in self.outbox if q["qid"] not in gone]
        self.bridge.outbox_drop(self.id, gone)

    async def _flush(self, items: list[dict], lead: str = "") -> None:
        """Сообщения — в ленту и агенту одним сообщением (несколько отложенных — одним, через пустую строку). lead — текст
        агенту перед ними («лимит восстановился — продолжай»); сообщений нет — в ленте «продолжай» (auto: resume)."""
        if self.status == "idle":
            self.turn_failed = self.stopped = False
        self._unqueue([q for q in items if q["qid"] in {o["qid"] for o in self.outbox}])
        answer, images = None, []
        if lead and not items:
            self._event({"kind": "user", "text": "", "auto": "resume"})
        said = "\n\n".join(q["text"] for q in items if q["text"])
        files = [f for q in items for f in (self.bridge.file_info(self.id, fid) for fid in q.get("files") or []) if f]
        for q in items:
            answer = q.get("answer") or answer
        if items:
            # режим ответа хода — в событии: сайт рисует ответ плеером, только если ход шёл в «Видео», а не по
            # нынешнему выбору под полем (иначе смена режима перекрашивала прежние ответы)
            self._event({"kind": "user", "text": said,
                         "files": [{k: f[k] for k in ("fid", "name", "mime", "size")} for f in files] or None,
                         **({"mode": str(answer.get("mode") or "text")} if answer else {})})
        text = said
        if files:
            text = (text or "Посмотри файлы.") + "\n\nФайлы от владельца:\n" + "\n".join(  # i18n: model
                f"- {f['path']} ({f['name']}, {f['mime']})" for f in files)
            images = [f["path"] for f in files if f["mime"].startswith("image/")]
        text = "\n\n".join(p for p in [lead, text] if p)
        text = self._with_intro(text) + self._answer_note(answer) + self._access_note()
        self._live({"kind": "session", "session": self.public()})
        await self._submit(text, images)

    async def _submit(self, text: str, images: list[str]) -> None:
        # Сайт получает ответ сразу (сообщение уже в ленте, «Работает»), а процесс claude поднимается фоном:
        # запуск с resume — секунды, и всё это время поле ввода висело с неотправленным текстом
        self._set_status("running")
        asyncio.create_task(self._deliver(text))

    answer_mode: str | None = None       # режим ответа, о котором агент уже знает в этом процессе моста

    def _answer_note(self, answer: dict | None) -> str:
        """Режим ответа на сайте («Текстом» / «Аудио» / «Видео») — агенту в конце сообщения, в ленту не попадает.
        Полные правила — при первом ходе и смене режима, дальше короткое напоминание. После рестарта моста про
        возобновлённую сессию не знаем, что агент уже слышал, — правила ещё раз (текстовый режим — короткой строкой)."""
        if not answer:
            return ""
        mode, last = str(answer.get("mode") or "text"), self.answer_mode
        self.answer_mode = mode
        if mode == last:
            note = str(answer.get("short") or "")
        elif mode == "text" and last is None and not self.row.get("claude_id"):
            note = ""                               # новая сессия в обычном режиме — говорить не о чем
        else:
            note = str(answer.get("rules") or "")
        return "\n\n" + note[:20000] if note else ""

    def _with_intro(self, text: str) -> str:
        """Первое сообщение сессии-продолжения: выжимка прежней сессии перед ним (один раз)."""
        intro = self.row.get("intro")
        if not intro:
            return text
        self.bridge.update(self.id, intro=None)
        return intro + text

    async def _deliver(self, text: str) -> None:
        try:
            await self._ensure()
            await self.client.query(text)
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] deliver", self.id)
            self._event({"kind": "error", "code": "start", "text": str(e)[:500]})
            self._set_status("idle")

    async def compact(self) -> None:
        """«Сжать контекст» с сайта (кэш остыл или вот-вот остынет): /compact Claude Code — без пометок режима ответа и
        доступа, они стали бы указаниями для выжимки."""
        if (self.row.get("agent") or "claude") != "claude" or self.status != "idle":
            raise BridgeError("compact: Claude Code, idle only")     # сайт кнопку иначе не показывает
        self.turn_failed = self.stopped = False
        await self._submit("/compact", [])          # конец — событие compact_boundary («Контекст сжат») и итог хода

    async def stop(self) -> None:
        self.stopped = True
        self.cancel_resume()
        for _kind, fut in list(self.pending.values()):
            if not fut.done():
                fut.set_result(None)
        if self.client and self.status != "idle":
            try:
                await self.client.interrupt()
            except Exception as e:  # noqa: BLE001
                log.info("[%s] interrupt: %s", self.id, e)
            self._event({"kind": "note", "text": "Остановлено"})

    async def configure(self, model: str | None, effort: str | None) -> None:
        changes = {}
        if model and model != self.row["model"]:
            if model not in MODELS:
                raise BridgeError("нет такой модели")
            changes["model"] = model
        if effort and effort != self.row["effort"]:
            if effort not in EFFORTS:
                raise BridgeError("нет такого уровня")
            changes["effort"] = effort
        if not changes:
            return
        self.bridge.update(self.id, **changes)
        if self.client:
            if "effort" in changes and self.status == "idle":
                await self.close()          # effort — параметр запуска: следующий ход поднимет процесс с новым
            elif "model" in changes:
                await self.client.set_model(MODELS[model][1])
        self._event({"kind": "note", "text": f"Дальше: {MODELS[self.row['model']][0]}, {self.row['effort']}"})


class Bridge:
    def __init__(self, data_dir: str = DATA):
        os.makedirs(data_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "bridge.db"), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(sessions)")}
        for name, ddl in COLUMNS.items():
            if name not in have:
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")
        self.sessions: dict[str, Session] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.limits: dict[str, dict] = {}           # лимиты подписки Claude по окнам: последнее от Claude Code
        self.limit_seen: set[tuple] = set()         # предупреждения, уже показанные в ленте
        self.auto_compacted: dict[str, float] = {}  # сессия → cache_at, по которому автосжатие уже было (не повторять)

    # ── база ────────────────────────────────────────────────────────────────

    def _row(self, sid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM sessions WHERE id = ? AND archived = 0", (sid,)).fetchone()
        return dict(row) if row else None

    def get(self, sid: str) -> Session:
        if sid not in self.sessions:
            row = self._row(sid)
            if not row:
                raise BridgeError("нет такой сессии")
            self.sessions[sid] = _session_class(row)(self, row)
        return self.sessions[sid]

    def update(self, sid: str, **fields) -> None:
        fields["updated"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE sessions SET {cols} WHERE id = ?", (*fields.values(), sid))
        if sid in self.sessions:
            self.sessions[sid].row.update(fields)
            if set(fields) & {"title", "model", "effort", "resume_at", "access", "cache_at"}:
                self.publish({"session": sid, "kind": "session", "session": self.sessions[sid].public()})

    def add_event(self, sid: str, data: dict) -> dict:
        ts = time.time()
        data = {k: v for k, v in data.items() if v is not None}
        cur = self.db.execute("INSERT INTO events (session, ts, data) VALUES (?, ?, ?)",
                              (sid, ts, json.dumps(data, ensure_ascii=False)))
        self.db.execute("UPDATE sessions SET updated = ? WHERE id = ?", (ts, sid))
        if sid in self.sessions:
            self.sessions[sid].row["updated"] = ts
        event = {"id": cur.lastrowid, "ts": ts, **data}
        self.publish({"session": sid, **event})
        return event

    # ── настройки моста (общие на все сессии) ────────────────────────────────

    def setting(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

    @property
    def auto_compact(self) -> bool:
        return self.setting("auto_compact") == "1"

    @property
    def chrome(self) -> bool:
        return self.setting("chrome") == "1"

    async def restart_idle(self) -> None:
        """Гасит процессы Claude Code, которые сейчас ничего не делают: следующее сообщение поднимет их с новыми
        настройками (resume). Идущий ход, ждущий ответа владельца и фоновые команды не трогаем — подхватят настройку,
        когда процесс погаснет сам (sweep)."""
        for session in list(self.sessions.values()):
            if (session.client and session.status == "idle" and not session.pending
                    and (session.row.get("agent") or "claude") == "claude"
                    and not await self._bg_commands(session.client)):
                log.info("[%s] settings changed — closing idle process", session.id)
                await session.close()

    # ── отложенные сообщения ────────────────────────────────────────────────

    def outbox(self, sid: str) -> list[dict]:
        rows = self.db.execute("SELECT data FROM outbox WHERE session = ? ORDER BY ts", (sid,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def outbox_put(self, sid: str, item: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO outbox (qid, session, ts, data) VALUES (?, ?, ?, ?)",
                        (item["qid"], sid, item["ts"], json.dumps(item, ensure_ascii=False)))

    def outbox_drop(self, sid: str, qids) -> None:
        self.db.executemany("DELETE FROM outbox WHERE session = ? AND qid = ?", [(sid, q) for q in qids])

    # ── лимиты подписки (общие на все сессии Claude) ────────────────────────

    def limit_state(self, kind: str, state: dict) -> None:
        self.limits[kind] = {**state, "ts": time.time()}
        self.publish({"kind": "limits", "limits": self.limits})

    def limit_once(self, key: tuple) -> bool:
        if key in self.limit_seen:
            return False
        self.limit_seen.add(key)
        return True

    def limit_rejected(self) -> dict | None:
        """Окно, в которое упёрлись (последнее rejected с будущим обновлением)."""
        now = time.time()
        hit = [v for v in self.limits.values() if v["status"] == "rejected" and (v.get("resets_at") or 0) > now]
        return max(hit, key=lambda v: v["ts"]) if hit else None

    def events(self, sid: str, before: int | None = None, limit: int = 400) -> tuple[list[dict], bool]:
        sql, args = "SELECT id, ts, data FROM events WHERE session = ?", [sid]
        if before:
            sql += " AND id < ?"
            args.append(before)
        rows = self.db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit + 1)).fetchall()
        more = len(rows) > limit
        out = [{"id": r["id"], "ts": r["ts"], **json.loads(r["data"])} for r in reversed(rows[:limit])]
        return out, more

    def listing(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM sessions WHERE archived = 0 ORDER BY updated DESC LIMIT 200").fetchall()
        out = []
        for row in rows:
            live = self.sessions.get(row["id"])
            out.append(live.public() if live else _session_class(dict(row))(self, dict(row)).public())
        return out

    def create(self, project: str, model: str | None, effort: str | None, agent: str | None = None,
               intro: str | None = None, origin: str | None = None, access: str | None = None,
               rules: dict | None = None) -> Session:
        card = projects.resolve(project)
        if not card:
            raise BridgeError("нет такого проекта")
        agent = agent or "claude"
        if agent not in agents.NAMES:
            raise BridgeError("нет такого агента")
        model = model if agents.valid_model(agent, model) else agents.DEFAULTS[agent][0]
        effort = effort if effort in EFFORTS else agents.DEFAULTS[agent][1]
        access = access if access in ACCESS else "full"
        rules = agent_rules.clean(rules)
        if rules and agent != "claude":
            raise BridgeError("права агента — только у Claude Code")  # i18n: internal
        if rules:                               # агент знает свои права с первого сообщения
            intro = agent_rules.summary(rules) + (intro or "")
        sid, now = uuid.uuid4().hex[:12], time.time()
        self.db.execute("INSERT INTO sessions (id, project, project_name, cwd, model, effort, created, updated, agent, "
                        "intro, origin, access, rules) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (sid, card["id"], card["name"], card["path"], model, effort, now, now, agent, intro, origin,
                         access, json.dumps(rules, ensure_ascii=False) if rules else None))
        session = self.get(sid)
        self.publish({"session": sid, "kind": "session", "session": session.public()})
        return session

    def digest(self, sid: str) -> dict:
        """Выжимка сессии для продолжения в другом агенте: сообщения владельца, итоговые ответы ходов (последний текст
        перед result), изменённые файлы. Логи команд и промежуточные шаги не передаются."""
        rows = self.db.execute("SELECT data FROM events WHERE session = ? ORDER BY id", (sid,)).fetchall()
        mine, answers, files, last = [], [], [], None
        for row in rows:
            ev = json.loads(row["data"])
            k = ev.get("kind")
            if ev.get("parent"):
                continue
            if k == "user" and (ev.get("text") or "").strip():
                mine.append(ev["text"].strip())
            elif k == "text":
                last = ev.get("text") or ""
            elif k == "result":
                if last and last.strip():
                    answers.append(last.strip())
                last = None
            elif k == "tool" and ev.get("name") in EDIT_TOOLS:
                path = (ev.get("input") or {}).get("file_path") or (ev.get("input") or {}).get("notebook_path")
                if path and path not in files:
                    files.append(path)
        if last and last.strip():
            answers.append(last.strip())
        return {"messages": mine, "answers": answers, "files": files}

    def handoff(self, sid: str, agent: str, model: str | None = None, effort: str | None = None) -> Session:
        """Продолжить сессию в другом агенте: новая сессия в том же проекте с выжимкой этой."""
        old = self.get(sid)
        row = old.row
        if agent not in agents.NAMES:
            raise BridgeError("нет такого агента")
        d = self.digest(sid)
        cwd = row["cwd"]
        rel = [p[len(cwd) + 1:] if p.startswith(cwd + "/") else p for p in d["files"]]
        name = agents.NAMES.get(row.get("agent") or "claude", "агента")
        parts = [f"[Продолжение беседы «{row['title'] or 'Новая сессия'}» из {name} в этом же проекте ({cwd}). Что было:"]
        if d["messages"]:
            parts.append("Сообщения владельца:\n" + "\n".join(f"{n}. {_trim(t, 1500)}" for n, t in
                                                           enumerate(d["messages"][-20:], 1)))
        if d["answers"]:
            parts.append("Итоговые ответы агента:\n" + "\n\n".join(f"— {_trim(t, 3000)}" for t in d["answers"][-6:]))
        if rel:
            parts.append("Изменённые файлы: " + ", ".join(rel[:60]))
        parts.append("Логи команд не передаются — нужно, посмотри сам (git status, git diff, git log).]\n\n"
                     "Новое сообщение владельца:\n")
        session = self.create(row["project"], model, effort, agent, intro="\n\n".join(parts), origin=sid,
                              access=old.access)
        session_title = (row["title"] or "Новая сессия") + " — продолжение"
        self.update(session.id, title=session_title[:120])
        self.add_event(session.id, {"kind": "handoff", "from": sid, "from_title": row["title"] or "Новая сессия",
                                    "from_agent": row.get("agent") or "claude", "messages": len(d["messages"]),
                                    "answers": len(d["answers"]), "files": rel[:60]})
        self.add_event(sid, {"kind": "note", "text": f"Продолжено в {agents.NAMES[agent]}: «{session_title}»",
                             "to": session.id})
        return session

    async def delete(self, sid: str) -> None:
        session = self.get(sid)
        await session.close()
        self.db.execute("UPDATE sessions SET archived = 1, resume_at = NULL WHERE id = ?", (sid,))
        self.db.execute("DELETE FROM outbox WHERE session = ?", (sid,))
        # лента и файлы удалённой сессии больше не открываются — не храним (карточка остаётся для handoff)
        self.db.execute("DELETE FROM events WHERE session = ?", (sid,))
        shutil.rmtree(os.path.join(FILES_DIR, sid), ignore_errors=True)
        self.sessions.pop(sid, None)
        self.publish({"session": sid, "kind": "deleted"})

    # ── файлы сессии: DATA/files/<сессия>/<fid>-<имя> ───────────────────────

    def _file_dir(self, sid: str) -> str:
        path = os.path.join(FILES_DIR, sid)
        os.makedirs(path, exist_ok=True)
        return path

    def save_file(self, sid: str, name: str, data: bytes) -> dict:
        """Файл владельца (или копия файла с сервера) → {fid, name, mime, size, path}."""
        if len(data) > MAX_FILE:
            raise BridgeError("файл больше 50 МБ")
        name = os.path.basename(name or "file").strip().replace("/", "_")[:120] or "file"
        name = "".join(ch for ch in name if ch.isprintable()) or "file"
        fid = uuid.uuid4().hex[:16]
        path = os.path.join(self._file_dir(sid), f"{fid}-{name}")
        with open(path, "wb") as f:
            f.write(data)
        return {"fid": fid, "name": name, "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "size": len(data), "path": path}

    def store_file(self, sid: str, src: str) -> dict:
        if not os.path.isfile(src):
            raise BridgeError(f"нет файла {src}")
        if os.path.getsize(src) > MAX_FILE:
            raise BridgeError("файл больше 50 МБ")
        with open(src, "rb") as f:
            return self.save_file(sid, os.path.basename(src), f.read())

    def file_info(self, sid: str, fid: str) -> dict | None:
        if not fid or not all(c in "0123456789abcdef" for c in fid):
            return None
        folder = os.path.join(FILES_DIR, sid)
        try:
            names = [n for n in os.listdir(folder) if n.startswith(fid + "-")]
        except OSError:
            return None
        if not names:
            return None
        path = os.path.join(folder, names[0])
        name = names[0][len(fid) + 1:]
        return {"fid": fid, "name": name, "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "size": os.path.getsize(path), "path": path}

    # ── живой поток ─────────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"kind": "resync"})

    @staticmethod
    async def _bg_commands(client) -> int:
        """Сколько фоновых команд у процесса Claude: инструмент Bash запускает каждую обёрткой
        «source ~/.claude/shell-snapshots/… && eval …» — прямой ребёнок CLI с этой строкой и есть команда (серверы MCP —
        без неё). Windows и неизвестный процесс — 0: там гасим, как раньше."""
        pid = getattr(getattr(getattr(client, "_transport", None), "_process", None), "pid", None)
        if not pid or os.name == "nt":
            return 0
        try:
            proc = await asyncio.create_subprocess_exec("ps", "-A", "-o", "ppid=,args=", stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
        except (OSError, asyncio.TimeoutError):
            return 0
        n = 0
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0] == str(pid) and "shell-snapshots" in parts[1]:
                n += 1
        return n

    async def sweep(self) -> None:
        """Гасит процессы, которые молчат IDLE_CLOSE_SEC и ничего не ждут; продолжает сессии, ждавшие лимита."""
        while True:
            await asyncio.sleep(30)
            due = self.db.execute("SELECT id FROM sessions WHERE resume_at IS NOT NULL AND resume_at <= ? "
                                  "AND archived = 0", (time.time(),)).fetchall()
            for row in due:
                try:
                    session = self.get(row["id"])
                    log.info("[%s] limit — resuming", session.id)
                    await session.resume(auto=True)
                except Exception:  # noqa: BLE001
                    log.exception("[%s] resume", row["id"])
                    self.update(row["id"], resume_at=None)
            try:
                await self._auto_compact()
            except Exception:  # noqa: BLE001
                log.exception("auto compact")
            now = time.monotonic()
            for session in list(self.sessions.values()):
                if (session.client and session.status == "idle" and not session.pending
                        and now - session.last_active > IDLE_CLOSE_SEC):
                    bg = await self._bg_commands(session.client)
                    if bg and now - session.last_active < BG_MAX_SEC:
                        if not getattr(session, "bg_logged", False):
                            log.info("[%s] idle with %d background commands — keeping it up", session.id, bg)
                            session.bg_logged = True
                        continue
                    log.info("[%s] idle — closing", session.id)
                    session.bg_logged = False
                    if bg:
                        session._event({"kind": "note", "code": "bg_stopped"})     # текст — на языке сайта (server.js noteText)
                    await session.close()

    async def _auto_compact(self) -> None:
        """Автосжатие: кэш контекста подписки (час) кончается через AUTO_COMPACT_LEAD_SEC — /compact, пока сжатие ещё
        читает контекст из кэша; следующее сообщение пойдёт с коротким контекстом, а не запишет весь заново. Кэш на
        5 минут (ключ API, докупка сверх лимита) не трогаем — сжимали бы сразу после каждого хода. По одному сроку кэша —
        одна попытка: не вышло (лимит, ошибка) — до следующего хода не повторяем."""
        if not self.auto_compact:
            return
        now = time.time()
        rows = self.db.execute(
            "SELECT id, cache_at FROM sessions WHERE archived = 0 AND agent = 'claude' AND cache_ttl >= 3600 "
            "AND ctx_tokens >= ? AND cache_at + cache_ttl - ? BETWEEN 0 AND ?",
            (AUTO_COMPACT_MIN_TOKENS, now, AUTO_COMPACT_LEAD_SEC)).fetchall()
        for row in rows:
            if self.auto_compacted.get(row["id"]) == row["cache_at"]:
                continue
            self.auto_compacted[row["id"]] = row["cache_at"]
            session = self.get(row["id"])
            if session.status != "idle" or session.pending or session.outbox or self.limit_rejected():
                continue
            log.info("[%s] cache expires soon — auto compact", session.id)
            session._event({"kind": "note", "code": "auto_compact"})      # текст — на языке сайта (server.js noteText)
            await session.compact()

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            if session.client:
                await session.close()


def _session_class(row: dict):
    agent = row.get("agent") or "claude"
    if agent == "codex":
        from .codex_session import CodexSession
        return CodexSession
    if agent in agents.ACP:
        from .acp_session import AcpSession
        return AcpSession
    if agent == "pi":
        from .pi_session import PiSession
        return PiSession
    return Session
