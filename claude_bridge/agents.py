"""Агенты моста: Claude Code, Codex и Antigravity на этой машине — установлен ли, вошёл ли, модели и вход по ссылке.

Каждый агент — свой CLI в домашней папке пользователя (тот же вход, что в терминале): Claude Code — `~/.local/bin/claude`
(сессии — Claude Agent SDK, sessions.py), Codex — `codex` из npm (сессии — `codex exec --json`, codex_session.py),
Antigravity CLI, Gemini CLI, OpenCode, Cursor и другие — по ACP (`agy acp`, `gemini --acp`, `opencode acp`…: acp.py,
acp_session.py, таблица ACP).

Antigravity CLI (`agy`, Google) — преемник Gemini CLI (с 18.06.2026 Gemini CLI личным аккаунтам Google не служит, остался
только у Gemini Code Assist для компаний): в списке машины всегда он, Gemini CLI — только если установлен. Вход —
`agy acp --login` в pty: печатает ссылку Google и ждёт код со страницы (вставляют на сайте), после входа выходит сам;
вход хранится в связке ключей системы (без неё — в файле), поэтому «вошёл ли» — проверкой ACP (session/new), как у
прочих ACP. Обновление — `agy update`.

Вход — тем же CLI, что и в терминале, но без терминала: процесс входа идёт в псевдотерминале (pty), мост читает из
него ссылку (и код устройства у Codex) и отдаёт сайту. Codex ждёт, пока человек введёт код на странице OpenAI, и сам
завершается; Claude Code после входа на claude.com показывает код, его вставляют на сайте — мост пишет его в pty.
Gemini CLI входит только из интерактивного режима (без терминала отказывает): мост запускает `gemini` в pty с
NO_BROWSER=true, тот печатает ссылку Google и ждёт код со страницы codeassist.google.com — его вставляют на сайте, как
у Claude Code; появился ~/.gemini/oauth_creds.json — вход есть, процесс гасим (дальше он открыл бы свой экран).
Ссылки и коды входа живут только в памяти моста и в ответе сайту, в журнал не пишутся.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import socket
import time
import tomllib

try:
    import pty                    # вход в агента через pty — Linux и Mac; на Windows входят в терминале
except ImportError:
    pty = None

log = logging.getLogger("claude_bridge")

HOME = os.path.expanduser("~")


def _cli(env: str, name: str, *usual: str) -> str:
    """Путь к CLI агента: из окружения, из обычных мест установки или из PATH (ПК владельца — не только сервер)."""
    if os.environ.get(env):
        return os.environ[env]
    for path in usual:
        if os.path.exists(path):
            return path
    return shutil.which(name) or usual[0]


CLAUDE_CLI = _cli("CLAUDE_CLI", "claude", os.path.join(HOME, ".local/bin/claude"), "/opt/homebrew/bin/claude",
                  "/usr/local/bin/claude")
CODEX_CLI = _cli("CODEX_CLI", "codex", os.path.join(HOME, ".npm-global/bin/codex"), "/opt/homebrew/bin/codex",
                 "/usr/local/bin/codex")
CODEX_HOME = os.environ.get("CODEX_HOME", os.path.join(HOME, ".codex"))
GEMINI_CLI = _cli("GEMINI_CLI", "gemini", os.path.join(HOME, ".npm-global/bin/gemini"), "/opt/homebrew/bin/gemini",
                  "/usr/local/bin/gemini")
GEMINI_HOME = os.path.join(HOME, ".gemini")
GEMINI_CREDS = os.path.join(GEMINI_HOME, "oauth_creds.json")
STATUS_TTL = 60
PROBE_TIMEOUT = 40

# Агенты по ACP (acp.py, acp_session.py) — команда запуска как в реестре ACP (cdn.agentclientprotocol.com) и у T3 Code.
# В списке агентов машины — только установленные (Antigravity — всегда). login — чем войти в терминале (вход с сайта —
# у Antigravity и Gemini CLI: Login ниже), paths — где CLI бывает кроме PATH (от домашней папки)
ACP = {
    "antigravity": {"name": "Antigravity", "bin": "agy", "args": ["acp"], "login": "agy acp --login",
                    "paths": [".local/bin/agy", "AppData/Local/agy/bin/agy.exe"]},
    "gemini": {"name": "Gemini CLI", "bin": "gemini", "args": ["--acp"], "login": "gemini"},
    # OpenCode сам разрешает себе правки и команды — просим спрашивать: решает мост по режиму сессии (acp_session)
    "opencode": {"name": "OpenCode", "bin": "opencode", "args": ["acp"], "login": "opencode auth login",
                 "paths": [".opencode/bin/opencode"],
                 "env": {"OPENCODE_CONFIG_CONTENT": json.dumps({"permission": {"edit": "ask", "bash": "ask"}})}},
    "cursor": {"name": "Cursor", "bin": "cursor-agent", "args": ["acp"], "login": "cursor-agent login"},
    "copilot": {"name": "GitHub Copilot", "bin": "copilot", "args": ["--acp"], "login": "copilot"},
    "qwen": {"name": "Qwen Code", "bin": "qwen", "args": ["--acp"], "login": "qwen"},
    "grok": {"name": "Grok", "bin": "grok", "args": ["agent", "stdio"], "login": "grok"},
    "kiro": {"name": "Kiro", "bin": "kiro-cli", "args": ["acp", "--agent-engine=v3"], "login": "kiro-cli login"},
    "goose": {"name": "goose", "bin": "goose", "args": ["acp"], "login": "goose configure"},
}


def _acp_cli(agent: str) -> str:
    spec = ACP[agent]
    name = spec["bin"]
    return _cli(f"YUNE_{agent.upper()}_CLI", name, *[os.path.join(HOME, p) for p in spec.get("paths") or []],
                os.path.join(HOME, ".npm-global/bin", name), os.path.join(HOME, ".local/bin", name),
                "/opt/homebrew/bin/" + name, "/usr/local/bin/" + name)
LOGIN_TTL = 15 * 60

API_KEY = "API key"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class AgentError(Exception):
    pass


def _codex_config() -> dict:
    try:
        with open(os.path.join(CODEX_HOME, "config.toml"), "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def codex_models() -> list[dict]:
    """Модели Codex — как в его списке (~/.codex/models_cache.json, visibility list) с его уровнями; модель из
    config.toml — первой, если её в списке нет (Codex работает на ней)."""
    try:
        with open(os.path.join(CODEX_HOME, "models_cache.json"), encoding="utf-8") as f:
            cache = [m for m in json.load(f).get("models") or [] if isinstance(m, dict) and m.get("slug")]
    except (OSError, ValueError, AttributeError):
        cache = []

    def one(m: dict) -> dict:
        return _model(m["slug"], m.get("display_name"), [lv.get("effort") if isinstance(lv, dict) else lv
                                                       for lv in m.get("supported_reasoning_levels") or []])
    out = [one(m) for m in sorted(cache, key=lambda m: m.get("priority") or 99) if m.get("visibility") == "list"]
    model = str(_codex_config().get("model") or "")
    if model and not any(x["id"] == model for x in out):
        out.insert(0, next((one(m) for m in cache if m["slug"] == model), _model(model, model)))
    return out


def codex_defaults() -> tuple[str, str]:
    """Модель и уровень Codex по умолчанию — из его config.toml; уровня там нет — тот, что Codex назначил модели."""
    cfg = _codex_config()
    model = str(cfg.get("model") or next((m["id"] for m in codex_models()), ""))
    effort = str(cfg.get("model_reasoning_effort") or "")
    if not effort:
        try:
            with open(os.path.join(CODEX_HOME, "models_cache.json"), encoding="utf-8") as f:
                effort = str(next((m.get("default_reasoning_level") for m in json.load(f).get("models") or []
                                   if isinstance(m, dict) and m.get("slug") == model), "") or "")
        except (OSError, ValueError, AttributeError):
            effort = ""
    return model, effort


def codex_context_window() -> int:
    return int(_codex_config().get("model_context_window") or 272000)


# Модели и уровни размышлений — как их отдаёт сам агент, без своих списков и подсказок (владелец 24.11.2026):
# Claude Code — ответ initialize (models: value, displayName, supportedEffortLevels), Codex — models_cache.json,
# агенты ACP — ответ session/new, Pi — get_available_models. Модель — {id, name, efforts[, resolved]}; efforts пуст —
# уровня нет; resolved (Claude Code resolvedModel) — какая модель на деле стоит за пунктом вроде «Default».
# Пока агент списка не дал, список пуст и модель не передаём: агент берёт свою.
_models: dict[str, list[dict]] = {}
_current: dict[str, str] = {}           # модель, которую агент сам назвал текущей (ACP currentModelId)


def _model(mid, name=None, efforts=(), resolved=None) -> dict:
    """resolved — модель, на которой агент реально работает под этим пунктом (Claude Code: «default» → claude-opus-…)."""
    out = {"id": str(mid), "name": str(name or mid), "efforts": [str(e) for e in efforts or [] if e]}
    if resolved:
        out["resolved"] = str(resolved)
    return out


def remember_models(agent: str, found: list[dict], current: str = "") -> None:
    if found:
        _models[agent] = found
    if current:
        _current[agent] = current


def remember_claude(found: list) -> None:
    """models из initialize Claude Code — как есть."""
    remember_models("claude", [_model(m["value"], m.get("displayName"), m.get("supportedEffortLevels"), m.get("resolvedModel"))
                               for m in found or [] if isinstance(m, dict) and m.get("value")])


async def claude_probe() -> None:
    """Список моделей Claude Code — из его initialize: процесс поднимается без запроса к модели."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    client = ClaudeSDKClient(ClaudeAgentOptions(cli_path=CLAUDE_CLI, cwd=HOME))
    try:
        await asyncio.wait_for(client.connect(), PROBE_TIMEOUT)
        remember_claude(((await client.get_server_info()) or {}).get("models") or [])
    except Exception as e:  # noqa: BLE001
        log.info("claude: model list: %s", e)
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def remember_pi_models(found: list) -> None:
    """Модели Pi (get_available_models) → «провайдер/модель»: так их и выбирают (set_model {provider, modelId})."""
    remember_models("pi", [_model(f"{m['provider']}/{m['id']}", m.get("name") or m["id"]) for m in found
                           if isinstance(m, dict) and m.get("provider") and m.get("id")])


def models(agent: str) -> list[dict]:
    if agent == "codex":
        return codex_models()
    return [dict(m) for m in _models.get(agent) or []]


def efforts(agent: str, model: str | None) -> list[str]:
    """Уровни размышлений модели — как их дал агент; нет модели в списке — нет и уровней."""
    return next((list(m["efforts"]) for m in models(agent) if m["id"] == model), [])


def defaults(agent: str) -> tuple[str, str]:
    """Модель и уровень новой сессии — то, что агент берёт сам: Claude Code — свой пункт default и effortLevel из его
    settings.json, Codex — config.toml, ACP — текущая модель из session/new. Пусто — не передаём, агент решит сам."""
    if agent == "codex":
        return codex_defaults()
    if agent == "claude":
        model = "default" if any(m["id"] == "default" for m in models(agent)) else ""
        try:
            with open(os.path.join(HOME, ".claude", "settings.json"), encoding="utf-8") as f:
                effort = str(json.load(f).get("effortLevel") or "")
        except (OSError, ValueError, AttributeError):
            effort = ""
        return model, effort if effort in efforts(agent, model) else ""
    return _current.get(agent, ""), ""


# Pi (pi.dev) — не ACP, свой RPC (pi_session.py); вход — в терминале: `pi`, потом /login
PI_CLI = _cli("YUNE_PI_CLI", "pi", os.path.join(HOME, ".npm-global/bin/pi"), os.path.join(HOME, ".local/bin/pi"),
              "/opt/homebrew/bin/pi", "/usr/local/bin/pi")
NAMES = {"claude": "Claude Code", "codex": "Codex", **{a: spec["name"] for a, spec in ACP.items()}, "pi": "Pi"}
CLIS = {"claude": CLAUDE_CLI, "codex": CODEX_CLI, "gemini": GEMINI_CLI,
        **{a: _acp_cli(a) for a in ACP if a != "gemini"}, "pi": PI_CLI}
ALWAYS_LISTED = ("claude", "codex", "antigravity")
SITE_LOGIN = ("claude", "codex", "gemini", "antigravity")      # вход с сайта (Login); остальные — в терминале


def acp_argv(agent: str) -> list[str]:
    return [CLIS[agent], *ACP[agent]["args"]]


def acp_auth_method(agent: str, methods: list) -> str | None:
    """Способ входа для authenticate, когда вход на машине уже есть: Gemini CLI — Google (oauth_creds.json) или ключ
    GEMINI_API_KEY. Других агентов вход без окна не умеет — None (войти в терминале)."""
    ids = {str(m.get("id")) for m in methods if isinstance(m, dict)}
    if agent == "antigravity":
        # вход Google agy берёт сам из связки ключей; authenticate oauth-personal без входа открыл бы браузер на машине
        return "gemini-api-key" if os.environ.get("GEMINI_API_KEY") and "gemini-api-key" in ids else None
    if agent == "gemini":
        if os.path.exists(GEMINI_CREDS) and "oauth-personal" in ids:
            return "oauth-personal"
        if os.environ.get("GEMINI_API_KEY") and "gemini-api-key" in ids:
            return "gemini-api-key"
    return None


def acp_env(agent: str) -> dict | None:
    extra = ACP[agent].get("env")
    return {**os.environ, **extra} if extra else None


_probed: dict[str, tuple[bool, str]] = {}


async def acp_probe(agent: str) -> tuple[bool, str]:
    """Вошёл ли агент ACP: initialize и session/new в домашней папке (без запроса к модели). Ответ session/new —
    заодно список моделей. Не вошёл — ошибка входа (код -32000 или слова про ключ и вход). Ход сессии тоже обновляет
    это (note_login), поэтому проверяем только при «Проверить» и после старта моста."""
    from . import acp
    proc = acp.AcpProc(acp_argv(agent), HOME, acp_env(agent), lambda m, p: None,
                       lambda m, p: _no_request())
    try:
        await proc.start()
        await proc.call("initialize", acp.initialize_params(), PROBE_TIMEOUT)
        result = await proc.call("session/new", {"cwd": HOME, "mcpServers": []}, PROBE_TIMEOUT)
        remember_models(agent, *acp.models_of(result))
        state = (True, "")
    except acp.AcpError as e:
        state = (False, "") if acp.is_auth_error(e) else (False, e.message[:120])
    except (OSError, asyncio.TimeoutError) as e:
        state = (False, str(e)[:120])
    finally:
        await proc.close()
    _probed[agent] = state
    return state


async def _no_request() -> dict:
    from . import acp
    raise acp.AcpError(-32601, "method not supported")


async def pi_probe() -> tuple[bool, str]:
    """Вошёл ли Pi: `pi --mode rpc --no-session`, get_available_models — есть модели с ключом или входом — вошёл."""
    try:
        proc = await asyncio.create_subprocess_exec(PI_CLI, "--mode", "rpc", "--no-session", cwd=HOME,
                                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024)
    except OSError as e:
        return False, str(e)[:120]
    try:
        proc.stdin.write(b'{"id":"p1","type":"get_available_models"}\n')
        await proc.stdin.drain()
        while True:
            raw = await asyncio.wait_for(proc.stdout.readline(), PROBE_TIMEOUT)
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") == "p1":
                found = (msg.get("data") or {}).get("models") or []
                remember_pi_models(found)
                _probed["pi"] = (bool(found), "")
                return _probed["pi"]
    except asyncio.TimeoutError:
        pass
    finally:
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
        await proc.wait()
    _probed["pi"] = (False, "")
    return _probed["pi"]


def note_login(agent: str, ok: bool) -> None:
    """Сессия ACP или Pi увидела, вошёл ли агент (session/new прошёл или упал на входе) — запомнить без новой проверки."""
    if (agent in ACP and agent != "gemini") or agent == "pi":
        _probed[agent] = (ok, "")
        _status.pop(agent, None)


def _gemini_settings_path() -> str:
    return os.path.join(GEMINI_HOME, "settings.json")


def gemini_login_state() -> tuple[bool, str]:
    """(вошёл ли, чем): вход Google (~/.gemini/oauth_creds.json, почта — google_accounts.json) или ключ GEMINI_API_KEY."""
    if os.path.exists(GEMINI_CREDS):
        try:
            with open(os.path.join(GEMINI_HOME, "google_accounts.json"), encoding="utf-8") as f:
                email = str((json.load(f) or {}).get("active") or "")
        except (OSError, ValueError, AttributeError):
            email = ""
        return True, email or "Google"
    if os.environ.get("GEMINI_API_KEY"):
        return True, API_KEY
    return False, ""


def gemini_prepare_login() -> None:
    """Вход Google выбирается в настройках Gemini CLI (security.auth.selectedType); без выбора он показал бы экран выбора
    способа входа. Ставим oauth-personal, только если способ ещё не выбран; остальное в файле не трогаем."""
    path = _gemini_settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        raise AgentError("Couldn't read ~/.gemini/settings.json — sign in in the terminal: gemini")
    if not isinstance(data, dict):
        raise AgentError("Couldn't read ~/.gemini/settings.json — sign in in the terminal: gemini")
    auth = data.setdefault("security", {}).setdefault("auth", {})
    if auth.get("selectedType"):
        return
    auth["selectedType"] = "oauth-personal"
    os.makedirs(GEMINI_HOME, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def valid_model(agent: str, model: str | None) -> bool:
    return any(m["id"] == model for m in models(agent))


def valid_effort(agent: str, model: str | None, effort: str | None) -> bool:
    return effort in efforts(agent, model)


async def _run(*args: str, timeout: float = 15) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL)
    except OSError as e:
        return 127, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode, ANSI.sub("", (out or b"").decode(errors="replace")).strip()


_status: dict[str, tuple[float, dict]] = {}

# Обновление агента (сайт: «Доступна новая версия — Обновить / Обновлять всегда»). Claude Code обновляет себя сам
# (`claude update`); агенты из npm — `npm install -g <пакет>@latest`, если стоят через npm (иначе — руками, как ставили).
# Пакет npm — и для сверки с последней версией на сайте (тот же список)
NPM = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex", "gemini": "@google/gemini-cli"}
SELF_UPDATE = ("claude", "antigravity")      # обновляют себя сами: `claude update`, `agy update`
UPDATE_TIMEOUT = 600


class UpdateError(Exception):
    """Обновить нельзя: code — manual (не через npm) | failed (команда упала) | unknown (агента нет)."""


async def update(agent: str) -> dict:
    """Обновить CLI агента → {agent, before, after}. Сессии агента вызывающий проверяет сам (server.agent_update)."""
    if agent not in (*NPM, *SELF_UPDATE) or not os.path.exists(CLIS.get(agent, "")):
        raise UpdateError("unknown")
    before = (await status(agent, fresh=True)).get("version")
    cli = CLIS[agent]
    if agent in SELF_UPDATE:
        cmd = [cli, "update"]
    else:
        npm = shutil.which("npm")
        if not npm or "node_modules" not in os.path.realpath(cli):
            raise UpdateError("manual")
        cmd = [npm, "install", "-g", NPM[agent] + "@latest"]
    code, out = await _run(*cmd, timeout=UPDATE_TIMEOUT)
    _status.pop(agent, None)
    after = (await status(agent, fresh=True)).get("version")
    if code != 0:
        log.warning("update %s: exit %s: %s", agent, code, out[-400:])
        raise UpdateError("failed")
    log.info("update %s: %s → %s", agent, before, after)
    return {"agent": agent, "before": before, "after": after}



async def status(agent: str, fresh: bool = False) -> dict:
    """{id, name, installed, version, logged_in, account, models, efforts, default_model, default_effort, login}."""
    cached = _status.get(agent)
    if cached and not fresh and time.monotonic() - cached[0] < STATUS_TTL:
        info = dict(cached[1])
    else:
        info = {"id": agent, "name": NAMES[agent], "installed": False, "version": None, "logged_in": False, "account": ""}
        cli = CLIS[agent]
        if os.path.exists(cli):
            code, out = await _run(cli, "--version")
            if code == 0:
                info["installed"] = True
                info["version"] = (re.search(r"\d+\.\d+\.\d+", out) or [None])[0]
        if info["installed"] and agent == "claude":
            if fresh or agent not in _models:
                await claude_probe()
            code, out = await _run(cli, "auth", "status")
            try:
                data = json.loads(out)
            except ValueError:
                data = {}
            info["logged_in"] = bool(data.get("loggedIn"))
            info["account"] = {"claude.ai": "Claude subscription", "console": "Anthropic Console"}.get(
                data.get("authMethod") or "", data.get("authMethod") or "")
        elif info["installed"] and agent == "gemini":
            info["logged_in"], info["account"] = gemini_login_state()
        elif info["installed"] and agent == "pi":
            if fresh or agent not in _probed:
                await pi_probe()
            info["logged_in"], info["account"] = _probed.get(agent, (False, ""))
        elif info["installed"] and agent in ACP:
            if fresh or agent not in _probed:
                await acp_probe(agent)
            info["logged_in"], info["account"] = _probed.get(agent, (False, ""))
            if info["account"] and not info["logged_in"]:
                info["account"] = ""            # текст ошибки проверки — в журнал моста, не в карточку

        elif info["installed"]:
            code, out = await _run(cli, "login", "status")
            info["logged_in"] = code == 0 and "logged in" in out.lower() and "not logged" not in out.lower()
            info["account"] = ("ChatGPT subscription" if "chatgpt" in out.lower()
                               else API_KEY if "api key" in out.lower() else "")
        _status[agent] = (time.monotonic(), info)
        info = dict(info)
    model, effort = defaults(agent)
    levels = efforts(agent, model)
    info.update(models=models(agent), efforts=levels, default_model=model, default_effort=effort if effort in levels else "",
                login=LOGINS[agent].public() if agent in LOGINS else None)
    return info


async def listing(fresh: bool = False) -> list[dict]:
    """Агенты машины: Claude Code, Codex, Antigravity — всегда (видно, чего нет), остальные ACP — если установлены."""
    found = list(await asyncio.gather(*(status(a, fresh) for a in NAMES)))
    return [a for a in found if a["id"] in ALWAYS_LISTED or a["installed"]]


def machine() -> dict:
    """Карточка машины для «Подключений»: имя, хост, система."""
    os_id = {"darwin": "macos"}.get(platform.system().lower(), platform.system().lower())
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            rel = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
        os_id = rel.get("ID", os_id).strip('"')
        pretty = rel.get("PRETTY_NAME", "").strip('"')
    except OSError:
        pretty = platform.platform()
    return {"host": socket.gethostname(), "os": os_id, "os_name": pretty}


# ── вход в агента ─────────────────────────────────────────────────────────────────────────────────────────────

class Login:
    """Один вход в агента: процесс CLI в pty, из вывода — ссылка (и код у Codex). state: starting → waiting →
    (code_sent) → done | failed | cancelled."""

    def __init__(self, agent: str):
        self.agent = agent
        self.state = "idle"
        self.url = ""
        self.code = ""              # код устройства (Codex): его вводят на странице OpenAI
        self.needs_code = False     # Claude Code и Gemini CLI: код со страницы входа вставляют на сайте
        self.error = ""
        self.started = 0.0
        self.proc: asyncio.subprocess.Process | None = None
        self.fd = -1
        self.output = ""
        self.task: asyncio.Task | None = None
        self.creds_mtime = 0.0      # Gemini CLI: вход есть, когда oauth_creds.json появился или обновился после старта

    def public(self) -> dict | None:
        if self.state == "idle":
            return None
        return {"state": self.state, "url": self.url, "code": self.code, "needs_code": self.needs_code,
                "error": self.error, "expires": int(self.started + LOGIN_TTL)}

    def active(self) -> bool:
        return self.state in ("starting", "waiting", "code_sent")

    async def start(self, force: bool = False) -> dict:
        """force — войти заново, хотя вход уже есть. Codex стирает ~/.codex/auth.json сразу при старте входа (проверено
        12.11.2026: вход сервера пропал от пробного старта) — без force к вошедшему агенту вход не запускаем."""
        if self.active():
            return self.public()
        if pty is None or self.agent not in SITE_LOGIN:
            raise AgentError("Sign in on this machine in the terminal: " + {"claude": "claude auth login", "codex": "codex login --device-auth", "pi": "pi → /login"}.get(
                self.agent, (ACP.get(self.agent) or {}).get("login") or self.agent))
        if not force and (await status(self.agent, fresh=True))["logged_in"]:
            raise AgentError("Already signed in — signing in again needs confirmation: the current sign-in will be erased")
        self.state, self.url, self.code, self.error, self.output = "starting", "", "", "", ""
        self.needs_code = self.agent in ("claude", "gemini", "antigravity")
        self.started = time.time()
        env = dict(os.environ, BROWSER="/bin/false", NO_COLOR="1", TERM="dumb")
        if self.agent == "claude":
            argv = [CLAUDE_CLI, "auth", "login", "--claudeai"]
        elif self.agent == "antigravity":
            # agy acp --login: ссылка Google → код со страницы → вход в связку ключей и выход (проверено 1.5.14)
            argv = [CLIS["antigravity"], "acp", "--login"]
            env.update(TERM="xterm-256color")
        elif self.agent == "gemini":
            try:
                gemini_prepare_login()
            except AgentError as e:
                self.state, self.error = "failed", str(e)
                return self.public()
            if force and os.path.exists(GEMINI_CREDS):
                # с прежним входом Gemini CLI ссылку не покажет — откладываем его, не вышло войти — вернём (_watch)
                os.replace(GEMINI_CREDS, GEMINI_CREDS + ".bak")
            self.creds_mtime = os.path.getmtime(GEMINI_CREDS) if os.path.exists(GEMINI_CREDS) else 0.0
            argv = [GEMINI_CLI, "--skip-trust"]
            env.update(NO_BROWSER="true", TERM="xterm-256color")    # с TERM=dumb экран Gemini CLI не рисуется
        else:
            argv = [CODEX_CLI, "login", "--device-auth"]
        master, slave = pty.openpty()
        if self.agent in ("gemini", "antigravity"):
            import fcntl
            import struct
            import termios
            # широкий терминал: ссылка входа (~600 знаков) не рвётся переносами экрана
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))
        try:
            self.proc = await asyncio.create_subprocess_exec(*argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                                                             cwd=HOME, start_new_session=True)
        except OSError as e:
            os.close(master)
            self.state, self.error = "failed", f"The agent didn't start: {e}"
            if self.agent == "gemini" and os.path.exists(GEMINI_CREDS + ".bak") and not os.path.exists(GEMINI_CREDS):
                os.replace(GEMINI_CREDS + ".bak", GEMINI_CREDS)
            return self.public()
        finally:
            os.close(slave)
        self.fd = master
        self.task = asyncio.create_task(self._watch())
        for _ in range(60):                                 # ссылка появляется за 1–3 с
            if self.url or not self.active():
                break
            await asyncio.sleep(0.25)
        if self.active() and not self.url:
            self.error = "The agent didn't show a sign-in link"
            await self.cancel(failed=True)
        return self.public()

    async def _watch(self) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def readable():
            try:
                data = os.read(self.fd, 8192)
            except OSError:
                data = b""
            queue.put_nowait(data)
            if not data:
                loop.remove_reader(self.fd)

        loop.add_reader(self.fd, readable)
        creds = asyncio.create_task(self._gemini_creds()) if self.agent == "gemini" else None
        try:
            while True:
                left = self.started + LOGIN_TTL - time.time()
                if left <= 0:
                    self.error = "Sign-in timed out"
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), left)
                except asyncio.TimeoutError:
                    continue
                if not data:
                    break
                self.output = (self.output + data.decode(errors="replace"))[-20000:]
                self._parse()
        finally:
            if creds:
                creds.cancel()
            try:
                loop.remove_reader(self.fd)
            except (ValueError, OSError):
                pass
            code = await self._reap()
            try:
                os.close(self.fd)
            except OSError:
                pass
            if self.state != "cancelled":
                ok = code == 0 or self.agent == "gemini"      # Gemini CLI гасим сами, когда вход уже записан
                if ok:
                    fresh = await status(self.agent, fresh=True)
                    ok = fresh["logged_in"]
                self.state = "done" if ok else "failed"
                if not ok and not self.error:
                    tail = [ln for ln in ANSI.sub("", self.output).splitlines() if ln.strip()][-1:]
                    self.error = (tail[0].strip() if tail else "Sign-in didn't finish")[:300]
            bak = GEMINI_CREDS + ".bak"
            if self.agent == "gemini" and os.path.exists(bak):
                if os.path.exists(GEMINI_CREDS):
                    os.remove(bak)
                else:
                    os.replace(bak, GEMINI_CREDS)           # новый вход не случился — прежний на место
                    _status.pop("gemini", None)
            log.info("sign-in to %s: %s", self.agent, self.state)

    async def _gemini_creds(self) -> None:
        """Gemini CLI после кода не выходит, а открывает свой экран: вход записан (oauth_creds.json новее старта) —
        гасим процесс, _watch увидит конец вывода и проверит вход."""
        while self.active():
            await asyncio.sleep(1)
            try:
                mtime = os.path.getmtime(GEMINI_CREDS)
            except OSError:
                continue
            if mtime > self.creds_mtime:
                await asyncio.sleep(1)                       # файл дописывается, почта — следом в google_accounts.json
                try:
                    self.proc.terminate()
                except (OSError, ProcessLookupError):
                    pass
                return

    async def _reap(self) -> int:
        """Дождаться процесса входа (он уже закрыл pty или его гасят) → код выхода."""
        try:
            return await asyncio.wait_for(self.proc.wait(), 5)
        except asyncio.TimeoutError:
            self.proc.kill()
            return await self.proc.wait()

    def _parse(self) -> None:
        if self.agent == "claude":
            raw = self.output
            m = re.search(r"https://claude\.(?:com|ai)/[^\s\x07\x1b]*oauth/authorize\?[^\s\x07\x1b]+", raw)
            if m and not self.url:
                self.url = m.group(0)
                self.state = "waiting"
            if re.search(r"(?i)login successful|logged in", ANSI.sub("", raw[-500:])):
                self.state = "code_sent"
            return
        if self.agent in ("gemini", "antigravity"):
            m = re.search(r"https://accounts\.google\.com/o/oauth2/[^\s\x07\x1b]+", ANSI.sub("", self.output))
            if m and not self.url:
                self.url = m.group(0)
                self.state = "waiting"
            return
        text = ANSI.sub("", self.output)
        if not self.url:
            m = re.search(r"https://auth\.openai\.com/\S+", text)
            if m:
                self.url = m.group(0)
        if not self.code:
            m = re.search(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,6})\b", text)
            if m:
                self.code = m.group(1)
        if self.url and self.code and self.state == "starting":
            self.state = "waiting"

    def send_code(self, code: str) -> dict:
        """Claude Code, Gemini CLI и Antigravity: код со страницы входа → в pty процесса входа."""
        code = code.strip()
        if self.agent not in ("claude", "gemini", "antigravity") or self.state != "waiting":
            raise AgentError("This sign-in isn't waiting for a code")
        if not code or len(code) > 400 or any(ch in code for ch in "\r\n"):
            raise AgentError("Paste the code from the sign-in page")
        os.write(self.fd, code.encode() + b"\r")
        self.state = "code_sent"
        return self.public()

    async def cancel(self, failed: bool = False) -> None:
        if not self.active():
            return
        if not failed:
            self.state = "cancelled"
        try:
            self.proc.terminate()
        except (OSError, ProcessLookupError):
            pass
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 6)
            except asyncio.TimeoutError:
                pass
        if failed:
            self.state = "failed"


LOGINS: dict[str, Login] = {a: Login(a) for a in NAMES}


def login(agent: str) -> Login:
    if agent not in LOGINS:
        raise AgentError("no such agent")
    return LOGINS[agent]
