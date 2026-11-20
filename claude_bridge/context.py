"""Стартовый контекст Claude Code в папке проекта — из чего он складывается, в токенах (сайт: «Агенты» → «Что видит
Claude Code при старте»).

Части, которые читаются из файлов: правила CLAUDE.md (глобальные ~/.claude, по пути к папке, с @-импортами), индекс
памяти MEMORY.md, строки скиллов («имя: описание»), MCP-серверы, добавка моста к системному промпту. Токены — оценка
по символам (ключа API у моста нет): кириллица ~2,6 знака на токен, латиница ~3,8, японский и китайский ~1.
База Claude Code (системный промпт и встроенные тулы) из файлов не видна — её даёт факт из журналов
~/.claude/projects/<папка>/*.jsonl: весь вход первого хода прошлых сессий минус части выше и первое сообщение.
"""
from __future__ import annotations

import glob
import json
import os
import re
import statistics

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
IMPORT = re.compile(r"(?m)^\s*@(\S+)")
SESSIONS = 8
TOP_SECTIONS = 8


def tokens(text: str) -> int:
    if not text or not text.strip():
        return 0
    cyr = len(re.findall(r"[\u0400-\u04ff]", text))           # кириллица
    cjk = len(re.findall(r"[぀-ヿ㐀-鿿＀-￯]", text))
    return max(1, round(cyr / 2.6 + cjk + (len(text) - cyr - cjk) / 3.8))


def short(path: str) -> str:
    return path.replace(HOME, "~", 1) if path.startswith(HOME) else path


# ── правила: CLAUDE.md по пути к папке и @-импорты (как Claude Code, до 5 уровней) ──────────────────────────────────
def _with_imports(path: str, seen: set, depth: int = 0) -> list[tuple[str, str]]:
    real = os.path.realpath(path)
    if real in seen or not os.path.isfile(real) or depth > 5:
        return []
    seen.add(real)
    try:
        text = open(real, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    out = [(path, text)]
    for ref in IMPORT.findall(re.sub(r"(?s)```.*?```", "", text)):
        ref = os.path.expanduser(ref)
        out += _with_imports(ref if os.path.isabs(ref) else os.path.join(os.path.dirname(real), ref), seen, depth + 1)
    return out


def rule_files(cwd: str) -> list[tuple[str, str]]:
    seen: set = set()
    out = _with_imports(os.path.join(CLAUDE, "CLAUDE.md"), seen)
    parts = os.path.abspath(cwd).split(os.sep)
    for i in range(1, len(parts) + 1):
        d = os.sep.join(parts[:i]) or os.sep
        for name in ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md")):
            out += _with_imports(os.path.join(d, name), seen)
    return out


def _sections(text: str) -> list[tuple[str, str]]:
    out, title, buf = [], "", []
    for line in text.split("\n"):
        if re.match(r"#{1,3} ", line):
            if buf:
                out.append((title, "\n".join(buf)))
            title, buf = line.lstrip("# ").strip()[:70], []
        buf.append(line)
    if buf:
        out.append((title, "\n".join(buf)))
    return out


def slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))


def memory_index(cwd: str) -> tuple[str, str] | None:
    p = os.path.join(CLAUDE, "projects", slug(cwd), "memory", "MEMORY.md")
    if os.path.isfile(p):
        return p, "".join(open(p, encoding="utf-8", errors="replace").readlines()[:200])
    return None


# ── скиллы и MCP ────────────────────────────────────────────────────────────────────────────────────────────────────
def _skill_line(path: str) -> str:
    text = open(path, encoding="utf-8", errors="replace").read()
    m = re.match(r"(?s)^---\n(.*?)\n---", text)
    head = m.group(1) if m else ""
    name = re.search(r"(?m)^name:\s*(.+)$", head)
    desc = re.search(r"(?ms)^description:\s*(.+?)(?=^\w[\w-]*:|\Z)", head)
    return f"- {name.group(1).strip() if name else os.path.basename(os.path.dirname(path))}: {(desc.group(1).strip() if desc else '')}"


def _settings() -> dict:
    try:
        return json.load(open(os.path.join(CLAUDE, "settings.json")))
    except (OSError, ValueError):
        return {}


def skills(cwd: str) -> list[dict]:
    """[{group, n, tokens}] — в контекст идёт только строка «имя: описание» каждого скилла."""
    groups = {"user": os.path.join(CLAUDE, "skills", "*", "SKILL.md"),
              "synced": os.path.join(CLAUDE, "skills", "synced", "*", "*", "SKILL.md"),
              "project": os.path.join(cwd, ".claude", "skills", "*", "SKILL.md")}
    for plug, on in (_settings().get("enabledPlugins") or {}).items():
        if on:
            name = plug.split("@")[0]
            groups["plugin:" + name] = os.path.join(CLAUDE, "plugins", "cache", "*", name, "*", "skills", "*", "SKILL.md")
    out = []
    for group, pattern in groups.items():
        lines = []
        for p in sorted(set(glob.glob(pattern))):
            try:
                lines.append(_skill_line(p))
            except OSError:
                continue
        if lines:
            out.append({"group": group, "n": len(lines), "tokens": tokens("\n".join(lines))})
    return out


def mcp_servers(cwd: str) -> dict:
    names = []
    try:
        cfg = json.load(open(os.path.join(HOME, ".claude.json")))
        names += list(cfg.get("mcpServers") or {})
        names += list(((cfg.get("projects") or {}).get(os.path.abspath(cwd), {}) or {}).get("mcpServers") or {})
    except (OSError, ValueError):
        pass
    p = os.path.join(cwd, ".mcp.json")
    if os.path.isfile(p):
        try:
            names += list(json.load(open(p)).get("mcpServers") or {})
        except ValueError:
            pass
    return {"servers": sorted(set(names)), "connectors_off": bool(_settings().get("disableClaudeAiConnectors"))}


# ── факт из журналов: первый ход прошлых сессий ──────────────────────────────────────────────────────────────────────
def first_turns(cwd: str, n: int = SESSIONS) -> list[dict]:
    files = sorted(glob.glob(os.path.join(CLAUDE, "projects", slug(cwd), "*.jsonl")), key=os.path.getmtime, reverse=True)
    out = []
    for f in files[:40]:
        msg, usage = None, None
        try:
            for line in open(f, encoding="utf-8", errors="replace"):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "user" and msg is None and not d.get("isMeta"):
                    c = (d.get("message") or {}).get("content")
                    msg = c if isinstance(c, str) else "\n".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                u = d["message"].get("usage") if isinstance(d.get("message"), dict) else None
                if u:
                    usage = u
                    break
        except OSError:
            continue
        if usage and msg is not None:
            total = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            out.append({"total": total, "msg": tokens(msg), "at": os.path.getmtime(f)})
        if len(out) >= n:
            break
    return out


def get(cwd: str, bridge_prompt: str = "") -> dict:
    """Разбор стартового контекста для папки cwd. bridge_prompt — добавка моста к системному промпту (sessions.py)."""
    cwd = os.path.abspath(os.path.expanduser(cwd))
    if not os.path.isdir(cwd):
        return {"error": "no_dir"}
    files = []
    for path, text in rule_files(cwd):
        secs = sorted(((tokens(t), title) for title, t in _sections(text)), reverse=True)[:TOP_SECTIONS]
        files.append({"path": short(path), "tokens": tokens(text), "global": os.path.realpath(path).startswith(os.path.realpath(CLAUDE)),
                      "sections": [{"title": title, "tokens": n} for n, title in secs if title]})
    mem = memory_index(cwd)
    memory = {"path": short(mem[0]), "tokens": tokens(mem[1]), "lines": mem[1].count("\n")} if mem else None
    sk = skills(cwd)
    mcp = mcp_servers(cwd)
    bridge = tokens(bridge_prompt)
    ours = sum(f["tokens"] for f in files) + (memory["tokens"] if memory else 0) + sum(s["tokens"] for s in sk) + bridge
    turns = first_turns(cwd)
    bases = [t["total"] - ours - t["msg"] for t in turns]
    base = int(statistics.median(bases)) if bases else None
    return {"path": short(cwd), "rules": files, "memory": memory, "skills": sk, "mcp": mcp, "bridge": bridge,
            "ours": ours, "base": base if base is None or base > 0 else None, "sessions": len(turns),
            "first": [t["total"] for t in turns][:SESSIONS], "estimate": True}
