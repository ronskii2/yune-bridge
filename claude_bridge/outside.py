"""Сессии Claude Code, начатые не через мост: в терминале, в VS Code, в приложении Claude.

Claude Code пишет каждую сессию в ~/.claude/projects/<папка>/<id>.jsonl. В строках есть cwd (папка), sessionId и
entrypoint: «cli», «claude-vscode», «claude-desktop» — сессия человека; «sdk-py», «sdk-ts», «sdk-cli» — программа (мост,
скрипты, `claude -p`), такие не показываем. Свои сессии моста (claude_id в базе) — тоже.

listing() — список для сайта: папка, название (ai-title, которое пишет сам Claude Code), начало первого сообщения,
время. events() — журнал в события ленты моста (user, text, tool, tool_result, result на конце хода): перенесённая
сессия открывается с историей, а продолжается тем же resume — с полным контекстом Claude.
"""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime

PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
LIST_MAX = 60           # сессий в списке
SCAN_MAX = 300          # журналов просматриваем (самые свежие)
FIRST_MAX = 150         # знаков первого сообщения — для названия и подписи
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REMINDER = re.compile(r"(?s)<system-reminder>.*?</system-reminder>")
# служебные вставки Claude Code в «сообщения человека»: команды (/clear, /model), их вывод, предупреждения
_SERVICE = ("<command-", "<local-command", "Caveat:", "[Request interrupted", "<bash-", "<user-memory-input")
_cache: dict[str, tuple[float, int, dict | None]] = {}


def _files() -> list[str]:
    out = []
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        if not _UUID.match(os.path.basename(path)[:-6]):
            continue
        try:
            out.append((os.path.getmtime(path), path))
        except OSError:
            continue
    out.sort(reverse=True)
    return [p for _, p in out[:SCAN_MAX]]


def path_of(claude_id: str) -> str | None:
    if not _UUID.match(claude_id or ""):
        return None
    found = glob.glob(os.path.join(PROJECTS, "*", claude_id + ".jsonl"))
    return found[0] if found else None


def user_text(message) -> str:
    """Текст человека из сообщения журнала: без напоминаний системы и служебных вставок; пусто — не сообщение человека."""
    content = (message or {}).get("content")
    if isinstance(content, list):
        content = "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return ""
    text = _REMINDER.sub("", content).strip()
    if not text or text.startswith(_SERVICE):
        return ""
    return text


def _ts(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _scan(path: str) -> dict | None:
    """Сводка журнала: id, папка, точка входа, название, первое сообщение, число сообщений человека."""
    sid, cwd, entry, title, first, n = os.path.basename(path)[:-6], "", "", "", "", 0
    with open(path, "rb") as f:
        for raw in f:
            # строки ответов и вызовов инструментов — самые длинные, разбираем только нужные
            if b'"type":"ai-title"' in raw[:200]:
                try:
                    title = str(json.loads(raw).get("aiTitle") or "").strip() or title
                except ValueError:
                    pass
                continue
            if b'"type":"user"' not in raw and not (not cwd and b'"cwd"' in raw):
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            cwd = cwd or str(d.get("cwd") or "")
            entry = entry or str(d.get("entrypoint") or "")
            if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
                continue
            text = user_text(d.get("message"))
            if text:
                n += 1
                first = first or " ".join(text.split())[:FIRST_MAX]
    if not cwd or not first:
        return None
    return {"id": sid, "cwd": cwd, "entrypoint": entry, "title": title[:120], "first": first, "messages": n}


def info(path: str) -> dict | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    got = _cache.get(path)
    if got and got[0] == st.st_mtime and got[1] == st.st_size:
        data = got[2]
    else:
        try:
            data = _scan(path)
        except OSError:
            data = None
        _cache[path] = (st.st_mtime, st.st_size, data)
    return {**data, "updated": st.st_mtime} if data else None


def listing(known: set[str]) -> list[dict]:
    """Сессии людей, которых нет в мосте, — самые свежие первыми."""
    out = []
    for path in _files():
        if os.path.basename(path)[:-6] in known:
            continue
        item = info(path)
        if not item or item["entrypoint"].startswith("sdk"):
            continue
        item["exists"] = os.path.isdir(item["cwd"])
        out.append(item)
        if len(out) >= LIST_MAX:
            break
    return out


def events(path: str) -> list[tuple[float | None, dict]]:
    """Журнал → события ленты моста по порядку. Ход закрывается событием result перед следующим сообщением человека
    (лента сворачивает законченные ходы по нему). Ветки субагентов (isSidechain) и мысли не переносим."""
    out: list[tuple[float | None, dict]] = []
    open_turn, last_ts = False, None

    def close():
        nonlocal open_turn
        if open_turn:
            out.append((last_ts, {"kind": "result", "imported": True}))
            open_turn = False

    with open(path, "rb") as f:
        for raw in f:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            kind = d.get("type")
            if kind not in ("user", "assistant", "system") or d.get("isSidechain"):
                continue
            ts = _ts(d.get("timestamp")) or last_ts
            last_ts = ts
            if kind == "system":
                if d.get("subtype") == "compact_boundary":
                    out.append((ts, {"kind": "note", "code": "compacted", "text": "Контекст сжат"}))  # i18n: internal
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            if kind == "user":
                if d.get("isMeta") or d.get("isCompactSummary"):
                    continue
                text = user_text(msg)
                if text:
                    close()
                    out.append((ts, {"kind": "user", "text": text}))
                    open_turn = True
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            out.append((ts, {"kind": "tool_result", "tid": b.get("tool_use_id"),
                                             "error": bool(b.get("is_error")), "content": b.get("content")}))
                continue
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and str(b.get("text") or "").strip():
                    out.append((ts, {"kind": "text", "text": b["text"]}))
                    open_turn = True
                elif b.get("type") == "tool_use":
                    out.append((ts, {"kind": "tool", "tid": b.get("id"), "name": b.get("name"),
                                     "input": b.get("input") if isinstance(b.get("input"), dict) else {}}))
                    open_turn = True
    close()
    return out
