"""Агенты проектов машины: реестр из проекта, вызовы ИИ в коде и задачи по расписанию — для страницы «Агенты» Yune.

Реестр — файл `.yune/agents.json` в корне проекта (живёт в git вместе с кодом):

  {"version": 1,
   "gateways": [{"name": "ai.py", "files": ["ai.py"], "calls": ["ai.complete(", "call_model("]}],
   "agents": [{"id": "model-scout", "name": "Разведчик моделей", "purpose": "раз в неделю ищет новые модели",
               "model": "service_models.SIMPLE", "files": ["model_scout.py"], "trigger": "timer|loop|user|event|manual",
               "schedule": "пн 10:00", "timer": "model-scout.timer"}],
   "ignore": [{"files": ["scripts/arena/*"], "reason": "бенчмарки, запускаются руками"}]}

  files — пути от корня проекта, можно с * (fnmatch: * проходит и через «/»); gateways — свои обёртки проекта над
  моделями: их вызовы (calls — подстроки) тоже ищем и тоже требуем записать в реестр.

Скан — без модели и без сети: в файлах проекта (git ls-files, у папки без git — обход с пропуском зависимостей) ищем
импорты SDK моделей, адреса API провайдеров (и свои шлюзы машины — расширение, AI_HTTP), вызовы обёрток проекта и зашитые id моделей. Вызов в файле,
который назван у агента реестра, — «зарегистрирован», в файле из ignore — «не агент», остальное — «не зарегистрирован».
Наружу уходят путь, строка и обрезанная строка кода, из которой вычищены похожие на ключи значения.

Расписание машины — таймеры systemd (системные и пользователя), crontab пользователя и /etc/cron.d: имя, когда,
прошлый запуск и команда без ключей (заголовки, KEY=…, длинные токены, query-строки вырезаны).

  GET /registry?fresh=1  →  {ok, at, projects: [...], timers: [...], errors}
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import subprocess
import time

from . import extension, projects as projects_mod

CACHE_SEC = 1800
MANIFEST = os.path.join(".yune", "agents.json")
MAX_FILES = 20000
MAX_BYTES = 1_000_000
MAX_CALLS = 300               # незарегистрированных на проект — дальше только счёт
SNIPPET = 160
CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rb", ".php", ".rs", ".java", ".kt", ".swift",
            ".sh", ".bash", ".cs", ".dart", ".ex", ".exs", ".lua", ".r", ".scala"}
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "env", "site-packages", "__pycache__", "dist", "build", ".next",
             ".nuxt", "vendor", "third_party", ".tox", ".mypy_cache", "coverage", ".cache", "target", "Pods"}
# минифицированное, чужое и тесты (тест — не агент: модели в нём зашиты нарочно)
SKIP_FILE = re.compile(r"(\.min\.(js|css)$|\.bundle\.js$|(^|/)vendor/|(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$"
                       r"|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$|(^|/)conftest\.py$)")

# импорты SDK: (провайдер, шаблон)
SDK = [
    ("openai", r"^\s*(from\s+openai\b|import\s+openai\b)|require\(\s*['\"]openai['\"]|from\s+['\"]openai['\"]"),
    ("anthropic", r"^\s*(from\s+anthropic\b|import\s+anthropic\b)|['\"]@anthropic-ai/sdk['\"]"),
    ("claude-agent-sdk", r"^\s*(from\s+claude_agent_sdk\b|import\s+claude_agent_sdk\b)|['\"]@anthropic-ai/claude-agent-sdk['\"]"),
    ("google", r"^\s*(from\s+google\s+import\s+genai|import\s+google\.generativeai|from\s+google\.generativeai|"
               r"from\s+google\.genai|import\s+google\.genai)|['\"]@google/(genai|generative-ai)['\"]"),
    ("mistral", r"^\s*(from\s+mistralai\b|import\s+mistralai\b)|['\"]@mistralai/mistralai['\"]"),
    ("cohere", r"^\s*(from\s+cohere\b|import\s+cohere\b)|require\(\s*['\"]cohere-ai['\"]|from\s+['\"]cohere-ai['\"]"),
    ("groq", r"^\s*(from\s+groq\b|import\s+groq\b)|['\"]groq-sdk['\"]"),
    ("ollama", r"^\s*(from\s+ollama\b|import\s+ollama\b)|from\s+['\"]ollama['\"]"),
    ("litellm", r"^\s*(from\s+litellm\b|import\s+litellm\b)"),
    ("langchain", r"^\s*(from\s+langchain\w*\b|import\s+langchain\w*\b)|['\"]@langchain/"),
    ("replicate", r"^\s*(from\s+replicate\b|import\s+replicate\b)|from\s+['\"]replicate['\"]"),
    ("ai-sdk", r"['\"]@ai-sdk/[a-z-]+['\"]|from\s+['\"]ai['\"]"),
    ("xai", r"^\s*(from\s+xai_sdk\b|import\s+xai_sdk\b)"),
    ("go-openai", r"['\"]github\.com/sashabaranov/go-openai['\"]|['\"]github\.com/openai/openai-go"),
]
# адреса API: (провайдер, шаблон)
HTTP = [
    ("openai", r"api\.openai\.com"), ("anthropic", r"api\.anthropic\.com"),
    ("google", r"generativelanguage\.googleapis\.com|aiplatform\.googleapis\.com"),
    ("openrouter", r"openrouter\.ai/api"), ("xai", r"api\.x\.ai\b"), ("deepseek", r"api\.deepseek\.com"),
    ("mistral", r"api\.mistral\.ai"), ("groq", r"api\.groq\.com"), ("together", r"api\.together\.(xyz|ai)"),
    ("replicate", r"api\.replicate\.com"), ("fireworks", r"api\.fireworks\.ai"), ("perplexity", r"api\.perplexity\.ai"),
    ("moonshot", r"api\.moonshot\.(cn|ai)"), ("dashscope", r"dashscope(-intl)?\.aliyuncs\.com"),
    ("cohere", r"api\.cohere\.(ai|com)"), ("elevenlabs", r"api\.elevenlabs\.io"), ("kie", r"api\.kie\.ai"),
    ("fal", r"\bfal\.run\b|queue\.fal\.run"), ("ollama", r"(localhost|127\.0\.0\.1):11434"),
    ("azure-openai", r"\.openai\.azure\.com"), ("bedrock", r"bedrock-runtime\.[a-z0-9-]+\.amazonaws\.com"),
]
# зашитый id модели в строке кода
MODEL = re.compile(r"""['"`](claude-(?:opus|sonnet|haiku|fable|3|instant)[\w.\-]*|gpt-[3-9][\w.\-]*|o[134](?:-mini|-pro)?"""
                   r"""|gemini-[0-9][\w.\-]*|grok-[0-9][\w.\-]*|deepseek-(?:chat|reasoner|v[0-9][\w.\-]*)|mistral-[a-z]+[\w.\-]*"""
                   r"""|llama-?[0-9][\w.\-]*|qwen[0-9][\w.\-]*|kimi-[\w.\-]+)['"`]""")
SDK_RE = [(p, re.compile(rx)) for p, rx in SDK]
# свои шлюзы моделей машины (расширение: AI_HTTP — [(имя, шаблон, подстрока для предфильтра)])
_EXTRA = extension.attr("AI_HTTP") or ()
HTTP_RE = [(p, re.compile(rx)) for p, rx in HTTP] + [(p, re.compile(rx)) for p, rx, _ in _EXTRA]
# предфильтр — подстроки в нижнем регистре: файл и строка без них регулярками не разбираются (иначе скан — минута)
KEYS = ("openai", "anthropic", "claude", "genai", "generativeai", "googleapis", "mistral", "cohere", "groq", "ollama",
        "litellm", "langchain", "replicate", "@ai-sdk", "'ai'", '"ai"', "xai", "x.ai", "openrouter", "deepseek",
        "together", "fireworks", "perplexity", "moonshot", "dashscope", "elevenlabs", "kie.ai", "fal.run", "11434",
        "bedrock", "gpt-", "gemini-", "grok-", "llama", "qwen", "kimi-", "'o1", '"o1', "'o3", '"o3', "'o4", '"o4')


def _maybe(text: str, extra: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in KEYS) or any(key in low for _, _, key in _EXTRA) or any(c in text for c in extra)

# вычистить из строки кода и команды всё похожее на ключ
SECRET_RE = [
    (re.compile(r"(sk-|sk_|pk_|rk_|xox[abp]-|ghp_|gho_|github_pat_|AIza|AKIA)[A-Za-z0-9_\-]{8,}"), r"\1…"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1…"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|auth)['\"]?\s*[:=]\s*['\"]?)[^\s'\",;]{6,}"), r"\1…"),
    (re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b"), "…"),     # хеши, токены
]
CMD_HEADER = re.compile(r"""(?:-H|--header)\s+(?:"[^"]*"|'[^']*'|\S+)""")
CMD_ENV = re.compile(r"\b([A-Z][A-Z0-9_]{2,})=(?:\"[^\"]*\"|'[^']*'|\S+)")
CMD_QUERY = re.compile(r"(https?://[^\s?'\"]+)\?[^\s'\"]+")

_cache: tuple[float, dict] | None = None
_lock = asyncio.Lock()


def redact(text: str) -> str:
    for rx, sub in SECRET_RE:
        text = rx.sub(sub, text)
    return text


def redact_cmd(cmd: str, limit: int = 220) -> str:
    cmd = CMD_HEADER.sub("-H …", cmd)
    cmd = CMD_ENV.sub(r"\1=…", cmd)
    cmd = CMD_QUERY.sub(r"\1?…", cmd)
    cmd = redact(" ".join(cmd.split()))
    return cmd if len(cmd) <= limit else cmd[:limit - 1] + "…"


async def get(fresh: bool = False) -> dict:
    global _cache
    async with _lock:
        if _cache and not fresh and time.time() - _cache[0] < CACHE_SEC:
            return _cache[1]
        errors: list[str] = []
        out = {"at": time.time(), "projects": [], "timers": [], "errors": errors}
        try:
            out["projects"] = await asyncio.to_thread(scan_all, errors)
        except Exception as e:  # noqa: BLE001 — расписание отдаём и без скана
            errors.append(f"projects: {type(e).__name__}: {str(e)[:200]}")
        try:
            out["timers"] = await timers()
        except Exception as e:  # noqa: BLE001
            errors.append(f"timers: {type(e).__name__}: {str(e)[:200]}")
        _link_timers(out["projects"], out["timers"])
        _cache = (time.time(), out)
        return out


# ── реестр проекта ──────────────────────────────────────────────────────────────────────────────────────────────

def _strs(v, limit: int = 40) -> list[str]:
    return [str(x)[:300] for x in v][:limit] if isinstance(v, list) else []


def _text(v, limit: int = 300) -> str:
    return " ".join(str(v).split())[:limit] if isinstance(v, (str, int, float)) else ""


def manifest(root: str) -> dict:
    """Реестр проекта: {found, error?, agents, gateways, ignore}. Кривые записи пропускаем, не роняя остальные."""
    path = os.path.join(root, MANIFEST)
    if not os.path.isfile(path):
        return {"found": False, "agents": [], "gateways": [], "ignore": []}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        return {"found": True, "error": f"{type(e).__name__}: {str(e)[:200]}", "agents": [], "gateways": [], "ignore": []}
    if not isinstance(raw, dict):
        return {"found": True, "error": "not an object", "agents": [], "gateways": [], "ignore": []}
    agents = []
    for i, a in enumerate(raw.get("agents") or []):
        if not isinstance(a, dict):
            continue
        agents.append({"id": _text(a.get("id"), 80) or f"agent-{i + 1}", "name": _text(a.get("name"), 120),
                       "purpose": _text(a.get("purpose"), 400), "model": _text(a.get("model"), 120),
                       "files": _strs(a.get("files")), "trigger": _text(a.get("trigger"), 20),
                       "schedule": _text(a.get("schedule"), 120), "timer": _text(a.get("timer"), 160)})
    gateways = [{"name": _text(g.get("name"), 120), "files": _strs(g.get("files")), "calls": _strs(g.get("calls"))}
                for g in raw.get("gateways") or [] if isinstance(g, dict)]
    ignore = [{"files": _strs(x.get("files")), "reason": _text(x.get("reason"), 200)}
              for x in raw.get("ignore") or [] if isinstance(x, dict)]
    return {"found": True, "agents": agents, "gateways": gateways, "ignore": ignore}


def _match(rel: str, globs: list[str]) -> bool:
    return any(rel == g or fnmatch.fnmatch(rel, g) for g in globs)


# ── скан кода ───────────────────────────────────────────────────────────────────────────────────────────────────

def _files(root: str) -> tuple[list[str], bool]:
    """Файлы кода проекта (пути от корня) и «обрезано ли». git — ls-files (уважает .gitignore), иначе обход."""
    rels: list[str] = []
    if os.path.exists(os.path.join(root, ".git")):
        try:
            out = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                 capture_output=True, timeout=30).stdout
            rels = [p for p in out.decode("utf-8", "replace").split("\0") if p]
        except (OSError, subprocess.SubprocessError):
            rels = []
    if not rels:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                rels.append(os.path.relpath(os.path.join(dirpath, name), root))
                if len(rels) > MAX_FILES * 3:
                    break
    rels = [r.replace(os.sep, "/") for r in rels]
    rels = [r for r in rels if os.path.splitext(r)[1].lower() in CODE_EXT and not SKIP_FILE.search(r)
            and not any(part in SKIP_DIRS for part in r.split("/")[:-1])]
    return rels[:MAX_FILES], len(rels) > MAX_FILES


def _scan_file(path: str, gateway_calls: list[str]) -> list[tuple[int, str, str, str]]:
    """[(строка, вид, провайдер, текст)] — вид: sdk | http | gateway | model."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return []
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return []
    if not _maybe(content, gateway_calls):
        return []
    lines = content.splitlines()
    hits = []
    for n, line in enumerate(lines, 1):
        if len(line) > 2000 or not _maybe(line, gateway_calls):      # длинное — минифицированное и данные, не код
            continue
        found = None
        for prov, rx in SDK_RE:
            if rx.search(line):
                found = ("sdk", prov)
                break
        if not found:
            for prov, rx in HTTP_RE:
                if rx.search(line):
                    found = ("http", prov)
                    break
        if not found and gateway_calls:
            for call in gateway_calls:
                if call in line and not line.lstrip().startswith(("def ", "async def ", "function ")):
                    found = ("gateway", call.rstrip("(").strip())
                    break
        if not found:
            m = MODEL.search(line)
            if m:
                found = ("model", m.group(1))
        if found:
            text = redact(line.strip())
            hits.append((n, found[0], found[1], text if len(text) <= SNIPPET else text[:SNIPPET - 1] + "…"))
    return hits


def scan(root: str) -> dict:
    """Один проект: реестр, вызовы ИИ по файлам и сверка с реестром."""
    man = manifest(root)
    files, cut = _files(root)
    gw_files = [g for gw in man["gateways"] for g in gw["files"]]
    gw_calls = [c for gw in man["gateways"] for c in gw["calls"] if len(c) >= 4]
    by_agent: dict[str, dict] = {a["id"]: {"files": set(), "calls": 0} for a in man["agents"]}
    unregistered, ignored, n_unreg = [], 0, 0
    gateways_hits = 0
    for rel in files:
        hits = _scan_file(os.path.join(root, rel), gw_calls)
        if not hits:
            continue
        if _match(rel, gw_files):           # сама обёртка проекта — её вызовы и есть «через шлюз»
            gateways_hits += len(hits)
            continue
        agent = next((a for a in man["agents"] if _match(rel, a["files"])), None)
        if agent:
            by_agent[agent["id"]]["files"].add(rel)
            by_agent[agent["id"]]["calls"] += len(hits)
            continue
        if any(_match(rel, x["files"]) for x in man["ignore"]):
            ignored += len(hits)
            continue
        for line, kind, prov, text in hits:
            n_unreg += 1
            if len(unregistered) < MAX_CALLS:
                unregistered.append({"file": rel, "line": line, "kind": kind, "provider": prov, "text": text})
    agents = [{**a, "hits": by_agent[a["id"]]["calls"], "found_files": sorted(by_agent[a["id"]]["files"])[:40]}
              for a in man["agents"]]
    return {"manifest": {k: man[k] for k in ("found", "error") if k in man}, "agents": agents,
            "gateways": man["gateways"], "ignore": man["ignore"], "unregistered": unregistered,
            "unregistered_count": n_unreg, "ignored_count": ignored, "gateway_count": gateways_hits,
            "files": len(files), "truncated": cut}


def scan_all(errors: list[str]) -> list[dict]:
    out = []
    for path in projects_mod.candidates():
        card = projects_mod._card(path)
        try:
            res = scan(path)
        except Exception as e:  # noqa: BLE001 — один проект не должен прятать остальные
            errors.append(f"{card['name']}: {type(e).__name__}: {str(e)[:160]}")
            continue
        out.append({"id": path, "name": card["name"], "path": path, **res})
    return out


# ── задачи по расписанию ────────────────────────────────────────────────────────────────────────────────────────

async def _run(*cmd: str, timeout: int = 30) -> str:
    if not shutil.which(cmd[0]):
        return ""
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                stdin=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ""
    return out.decode("utf-8", "replace")


def _blocks(text: str) -> list[dict]:
    """Вывод `systemctl show a b …` — блоки «ключ=значение» через пустую строку."""
    out, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                out.append(cur)
            cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        out.append(cur)
    return out


ARGV = re.compile(r"argv\[\]=([^;]*)")


def _usec(v) -> float | None:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v / 1e6 if v > 0 else None


async def _systemd(scope: tuple[str, ...]) -> list[dict]:
    raw = await _run("systemctl", *scope, "list-timers", "--all", "--output=json")
    try:
        items = json.loads(raw) if raw.strip() else []
    except ValueError:
        return []
    items = [x for x in items if isinstance(x, dict) and x.get("unit")]
    if not items:
        return []
    timers = {b.get("Id"): b for b in _blocks(await _run(
        "systemctl", *scope, "show", *[x["unit"] for x in items], "-p", "Id", "-p", "TimersCalendar",
        "-p", "TimersMonotonic", "-p", "Description"))}
    services = [x.get("activates") for x in items if x.get("activates")]
    units = {b.get("Id"): b for b in _blocks(await _run(
        "systemctl", *scope, "show", *services, "-p", "Id", "-p", "ExecStart", "-p", "WorkingDirectory",
        "-p", "Description", "-p", "Result", "-p", "ExecMainStatus"))} if services else {}
    out = []
    for x in items:
        t, s = timers.get(x["unit"], {}), units.get(x.get("activates"), {})
        cal = re.findall(r"OnCalendar=([^;]+?)\s*;", t.get("TimersCalendar", ""))
        mono = re.findall(r"\{ (OnUnitActiveUSec|OnBootUSec|OnActiveUSec|OnStartupUSec)=([^;]+?)\s*;", t.get("TimersMonotonic", ""))
        when = ", ".join(c.strip() for c in cal) or ", ".join(f"{k.replace('USec', '')} {v.strip()}" for k, v in mono)
        argv = ARGV.search(s.get("ExecStart", ""))
        status = s.get("ExecMainStatus")
        out.append({"kind": "systemd" + ("-user" if scope else ""), "id": x["unit"], "unit": x.get("activates") or "",
                    "name": (s.get("Description") or t.get("Description") or x["unit"])[:160], "when": when[:160],
                    "last": _usec(x.get("last")), "next": _usec(x.get("next")),
                    "result": s.get("Result") or "", "status": int(status) if str(status or "").isdigit() else None,
                    "cwd": (s.get("WorkingDirectory") or "")[:300],
                    "command": redact_cmd(argv.group(1).strip()) if argv else ""})
    return out


CRON_LINE = re.compile(r"^\s*(@\w+|(?:\S+\s+){4}\S+)\s+(.+)$")


def _cron_lines(text: str, kind: str, system: bool) -> list[dict]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or re.match(r"^[A-Z_][A-Z0-9_]*\s*=", line):
            continue
        m = CRON_LINE.match(line)
        if not m:
            continue
        when, cmd = m.group(1), m.group(2)
        if system and not when.startswith("@"):
            cmd = cmd.split(None, 1)[1] if len(cmd.split(None, 1)) > 1 else cmd     # /etc/cron.d: после времени — пользователь
        out.append({"kind": kind, "id": f"{kind}:{len(out)}", "name": "", "when": when, "last": None, "next": None,
                    "result": "", "status": None, "cwd": "", "command": redact_cmd(cmd)})
    return out


async def _cron() -> list[dict]:
    out = _cron_lines(await _run("crontab", "-l"), "cron", False)
    cron_d = "/etc/cron.d"
    if os.path.isdir(cron_d):
        for name in sorted(os.listdir(cron_d)):
            path = os.path.join(cron_d, name)
            if name.startswith(".") or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    items = _cron_lines(f.read(), "cron.d", True)
            except OSError:
                continue
            for i, it in enumerate(items):
                it["id"], it["name"] = f"cron.d/{name}:{i}", name
            out += items
    return out


async def timers() -> list[dict]:
    out: list[dict] = []
    if shutil.which("systemctl"):
        out += await _systemd(())
        out += await _systemd(("--user",))
    out += await _cron()
    return out


def _link_timers(projects: list[dict], items: list[dict]) -> None:
    """Таймер — к проекту, если команда или рабочая папка в его папке; к агенту — по полю timer реестра."""
    roots = sorted(((p["path"].rstrip("/") + "/", p) for p in projects), key=lambda x: -len(x[0]))
    for it in items:
        hay = f"{it.get('cwd') or ''}/ {it.get('command') or ''}"
        for prefix, p in roots:
            if prefix in hay:
                it["project"] = p["id"]
                break
        for p in projects:
            for a in p["agents"]:
                key = a.get("timer")
                if key and (key == it["id"] or key == it.get("unit") or key in (it.get("command") or "")):
                    it["project"], it["agent"] = p["id"], a["id"]
