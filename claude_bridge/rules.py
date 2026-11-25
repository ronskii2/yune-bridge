"""Права агента в сессии Claude Code: какие файлы читать и писать, какие команды выполнять сам, с вопросом или никогда.

Права задаёт и хранит сайт (агент Yune — «Агенты» → «Права»), мост получает их при создании сессии (POST /sessions,
поле rules) и проверяет каждый вызов инструмента в хуке PreToolUse (sessions.Session._pre_tool): права не в файлах
проекта — агент не может расширить их себе сам.

  {"files": {"read": ["**"], "write": ["reports/**"], "deny": [".env", "**/*.key", "~/.ssh/**"]},
   "commands": {"default": "self | ask | no", "rules": [{"match": "git push", "mode": "ask"}, ...]}}

files — шаблоны путей (fnmatch; «**» — всё): без «/» в начале и без «~» — от корня проекта, иначе абсолютные. Путь вне
read — нельзя читать, вне write — нельзя писать; deny — нельзя ничего, сильнее остального.
commands — команды Bash: правило подходит, если команда (каждая часть через &&, ||, ;, |) начинается с match; из частей
берётся самое строгое; не подошло ни одно — default. Команда, где назван закрытый путь (.env), — нельзя.
Инструменты MCP: чтение — можно, остальное — как default команд.

decide(rules, tool, input, cwd) → ("allow" | "ask" | "deny", причина для агента).
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex

MODES = ("self", "ask", "no")
READ_FILE_TOOLS = {"Read": ("file_path",), "NotebookRead": ("notebook_path",), "Glob": ("path",), "Grep": ("path",),
                   "LS": ("path",)}
WRITE_FILE_TOOLS = {"Edit": ("file_path",), "MultiEdit": ("file_path",), "Write": ("file_path",),
                    "NotebookEdit": ("notebook_path",)}
SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
STRICT = {"self": 0, "ask": 1, "no": 2}
ENV_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
REDIRECT = re.compile(r"(?:\d?>>?|\btee(?:\s+-a)?)\s*([^\s;&|<>]+)")


def clean(raw) -> dict | None:
    """Права с сайта → проверенный вид (лишнее и кривое отбрасываем). None — прав нет (обычная сессия)."""
    if not isinstance(raw, dict):
        return None

    def globs(v) -> list[str]:
        return [str(x).strip()[:300] for x in v if str(x).strip()][:60] if isinstance(v, list) else []

    files = raw.get("files") if isinstance(raw.get("files"), dict) else {}
    cmds = raw.get("commands") if isinstance(raw.get("commands"), dict) else {}
    rules = []
    for r in cmds.get("rules") or []:
        if isinstance(r, dict) and str(r.get("match") or "").strip() and r.get("mode") in MODES:
            rules.append({"match": " ".join(str(r["match"]).split())[:200], "mode": r["mode"]})
    return {"files": {"read": globs(files.get("read")) or ["**"], "write": globs(files.get("write")),
                      "deny": globs(files.get("deny"))},
            "commands": {"default": cmds.get("default") if cmds.get("default") in MODES else "ask", "rules": rules[:60]}}


def _abs(path: str, cwd: str) -> str:
    path = os.path.expanduser(str(path or "").strip() or cwd)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _match(path: str, patterns: list[str], cwd: str) -> bool:
    """Путь (абсолютный) подходит под шаблон: «dir/**» — папка целиком, «*.key» — по имени где угодно в проекте,
    «**/x» — x в любой папке, «/abs» и «~/…» — от корня машины."""
    root = os.path.normpath(cwd)
    rel = os.path.relpath(path, root).replace(os.sep, "/") if _under(path, root) else None
    for p in patterns:
        if p.startswith(("/", "~")):
            full = os.path.normpath(os.path.expanduser(p[:-3] if p.endswith("/**") else p))
            if (p.endswith("/**") and _under(path, full)) or path == full or fnmatch.fnmatch(path, full):
                return True
            continue
        if rel is None:
            continue
        if p in ("**", "*", "./**"):
            return True
        q = p[2:] if p.startswith("./") else p
        if q.endswith("/**") and _under(rel, q[:-3]):
            return True
        for cand in (q, q[3:] if q.startswith("**/") else None):
            if cand and (fnmatch.fnmatch(rel, cand) or ("/" not in cand and fnmatch.fnmatch(rel.rsplit("/", 1)[-1], cand))):
                return True
    return False


def _file(rules: dict, path: str, cwd: str, write: bool) -> tuple[str, str]:
    f = rules["files"]
    if _match(path, f["deny"], cwd):
        return "deny", f"Blocked by agent permissions: {path}"
    if write:
        return ("allow", "") if _match(path, f["write"], cwd) else ("deny", f"Agent permissions don't allow writing here: {path}. Allowed: {', '.join(f['write']) or 'nowhere'}")
    return ("allow", "") if _match(path, f["read"], cwd) else ("deny", f"Agent permissions don't allow reading this: {path}")


def _command_mode(rules: dict, command: str, cwd: str) -> tuple[str, str]:
    c = rules["commands"]
    worst, why = "self", ""
    for part in [p for p in SPLIT.split(command) if p.strip()]:
        words = " ".join(ENV_PREFIX.sub("", part.strip()).split())
        mode, rule = c["default"], ""
        best = -1
        for r in c["rules"]:
            m = r["match"]
            if (words == m or words.startswith(m + " ")) and len(m) > best:
                mode, rule, best = r["mode"], m, len(m)
        if STRICT[mode] > STRICT[worst]:
            worst, why = mode, rule or "default"
    # запись перенаправлением и tee — как запись файла: только в write
    for target in REDIRECT.findall(command):
        verdict, _ = _file(rules, _abs(target.strip("'\""), cwd), cwd, True)
        if verdict == "deny" and target not in ("/dev/null", "&1", "&2"):
            return "no", f"writing to {target}"
    # закрытые пути, названные в команде (cat .env) — нельзя
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    for t in tokens:
        if t.startswith("-") or ("/" not in t and "." not in t):
            continue
        if _match(_abs(t, cwd), rules["files"]["deny"], cwd):
            return "no", f"blocked path {t}"
    return worst, why


def decide(rules: dict, tool: str, inp: dict, cwd: str, reads: bool) -> tuple[str, str]:
    inp = inp if isinstance(inp, dict) else {}
    if tool in WRITE_FILE_TOOLS or tool in READ_FILE_TOOLS:
        keys = WRITE_FILE_TOOLS.get(tool) or READ_FILE_TOOLS[tool]
        path = next((inp.get(k) for k in keys if inp.get(k)), None)
        return _file(rules, _abs(path or cwd, cwd), cwd, tool in WRITE_FILE_TOOLS)
    if tool == "Bash":
        mode, why = _command_mode(rules, str(inp.get("command") or ""), cwd)
        if mode == "no":
            return "deny", f"Command forbidden by agent permissions ({why})."
        return ("ask", why) if mode == "ask" else ("allow", "")
    if reads:
        return "allow", ""
    mode = rules["commands"]["default"]
    if mode == "no":
        return "deny", f"{tool}: forbidden by agent permissions."
    return ("ask", "") if mode == "ask" else ("allow", "")


def summary(rules: dict) -> str:  # i18n: model
    """Права одной строкой для агента — в первое сообщение сессии."""
    f, c = rules["files"], rules["commands"]
    mode = {"self": "on its own", "ask": "with the owner's confirmation", "no": "not allowed"}
    parts = [f"read: {', '.join(f['read'])}", f"write: {', '.join(f['write']) or 'nowhere'}"]
    if f["deny"]:
        parts.append(f"blocked: {', '.join(f['deny'])}")
    parts.append(f"commands by default — {mode[c['default']]}")
    for r in c["rules"]:
        parts.append(f"“{r['match']}” — {mode[r['mode']]}")
    return ("[Agent permissions (the bridge checks them on every action, they can't be bypassed; if you need more, tell "
            "the owner what and why): " + "; ".join(parts) + ".]\n\n")
