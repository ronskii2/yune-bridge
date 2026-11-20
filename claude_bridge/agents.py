"""Агенты моста: Claude Code, Codex и Gemini CLI на этой машине — установлен ли, вошёл ли, модели и вход по ссылке.

Каждый агент — свой CLI в домашней папке пользователя (тот же вход, что в терминале): Claude Code — `~/.local/bin/claude`
(сессии — Claude Agent SDK, sessions.py), Codex — `codex` из npm (сессии — `codex exec --json`, codex_session.py),
Gemini CLI, OpenCode, Cursor и другие — по ACP (`gemini --acp`, `opencode acp`…: acp.py, acp_session.py, таблица ACP).

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
# В списке агентов машины — только установленные (Gemini CLI — всегда: его ставят чаще прочих). login — чем войти в
# терминале (вход с сайта — пока только у Gemini CLI: Login ниже), paths — где CLI бывает кроме PATH
ACP = {
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

CLAUDE_MODELS = {"opus": ("Opus 5.5", "claude-opus-5-5"), "sonnet": ("Sonnet 5.5", "claude-sonnet-5-5"),
                 "fable": ("Fable 5.1", "claude-fable-5-1"), "haiku": ("Haiku 5.5", "claude-haiku-5-5")}
DEFAULT_NAME = "По умолчанию"
API_KEY = "ключ API"
CLAUDE_HINTS = {"opus": "Самая сильная, по умолчанию", "sonnet": "Быстрее и дешевле", "fable": "Fable",
                "haiku": "Самая быстрая"}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
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
    """Модели Codex: «по умолчанию» (модель из ~/.codex/config.toml) и те, что Codex показывает в своём списке."""
    default = _codex_config().get("model") or ""
    out = [{"id": "default", "name": DEFAULT_NAME, "hint": default}]
    try:
        with open(os.path.join(CODEX_HOME, "models_cache.json"), encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    for m in sorted(cache.get("models") or [], key=lambda m: m.get("priority") or 99):
        if m.get("visibility") == "list" and m.get("slug"):
            out.append({"id": m["slug"], "name": m.get("display_name") or m["slug"], "hint": m.get("description") or ""})
    return out


def codex_context_window() -> int:
    return int(_codex_config().get("model_context_window") or 272000)


# Gemini CLI — псевдонимы моделей (-m pro|flash|flash-lite): CLI сам ведёт их на текущие модели, список не устаревает.
# Подсказки — те же строки, что у Claude (их переводит каталог сайта): у моста нет своих переводов
GEMINI_MODELS = [{"id": "default", "name": DEFAULT_NAME, "hint": "auto"},
                 {"id": "pro", "name": "Gemini Pro", "hint": ""},
                 {"id": "flash", "name": "Gemini Flash", "hint": CLAUDE_HINTS["sonnet"]},
                 {"id": "flash-lite", "name": "Gemini Flash Lite", "hint": CLAUDE_HINTS["haiku"]}]


# Модели агентов ACP — из ответа session/new (проверка входа или сессия): у каждого свой список
_acp_models: dict[str, list[dict]] = {}


def remember_models(agent: str, found: list[dict]) -> None:
    if found:
        _acp_models[agent] = found


def remember_pi_models(found: list) -> None:
    """Модели Pi (get_available_models) → «провайдер/модель»: так их и выбирают (set_model {provider, modelId})."""
    remember_models("pi", [{"id": f"{m['provider']}/{m['id']}", "name": str(m.get("name") or m["id"]),
                            "hint": str(m.get("provider") or "")} for m in found
                           if isinstance(m, dict) and m.get("provider") and m.get("id")])


def models(agent: str) -> list[dict]:
    if agent == "codex":
        return codex_models()
    if agent == "gemini" and not _acp_models.get(agent):
        return [dict(m) for m in GEMINI_MODELS]
    if agent in ACP or agent == "pi":
        return [{"id": "default", "name": DEFAULT_NAME, "hint": ""}] + [dict(m) for m in _acp_models.get(agent) or []]
    return [{"id": k, "name": v[0], "hint": CLAUDE_HINTS.get(k, "")} for k, v in CLAUDE_MODELS.items()]


# Pi (pi.dev) — не ACP, свой RPC (pi_session.py); вход — в терминале: `pi`, потом /login
PI_CLI = _cli("YUNE_PI_CLI", "pi", os.path.join(HOME, ".npm-global/bin/pi"), os.path.join(HOME, ".local/bin/pi"),
              "/opt/homebrew/bin/pi", "/usr/local/bin/pi")
DEFAULTS = {"claude": ("opus", "high"), "codex": ("default", "high"), **{a: ("default", "high") for a in ACP},
            "pi": ("default", "high")}
NAMES = {"claude": "Claude Code", "codex": "Codex", **{a: spec["name"] for a, spec in ACP.items()}, "pi": "Pi"}
CLIS = {"claude": CLAUDE_CLI, "codex": CODEX_CLI, "gemini": GEMINI_CLI,
        **{a: _acp_cli(a) for a in ACP if a != "gemini"}, "pi": PI_CLI}
NO_EFFORTS = (*ACP, "pi")           # уровня размышлений в меню у них нет
ALWAYS_LISTED = ("claude", "codex", "gemini")


def acp_argv(agent: str) -> list[str]:
    return [CLIS[agent], *ACP[agent]["args"]]


def acp_auth_method(agent: str, methods: list) -> str | None:
    """Способ входа для authenticate, когда вход на машине уже есть: Gemini CLI — Google (oauth_creds.json) или ключ
    GEMINI_API_KEY. Других агентов вход без окна не умеет — None (войти в терминале)."""
    ids = {str(m.get("id")) for m in methods if isinstance(m, dict)}
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
        remember_models(agent, acp.models_of(result)[0])
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
        raise AgentError("Не разобрала ~/.gemini/settings.json — войдите в терминале: gemini")
    if not isinstance(data, dict):
        raise AgentError("Не разобрала ~/.gemini/settings.json — войдите в терминале: gemini")
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
            code, out = await _run(cli, "auth", "status")
            try:
                data = json.loads(out)
            except ValueError:
                data = {}
            info["logged_in"] = bool(data.get("loggedIn"))
            info["account"] = {"claude.ai": "подписка Claude", "console": "Anthropic Console"}.get(
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
            info["account"] = ("подписка ChatGPT" if "chatgpt" in out.lower()
                               else API_KEY if "api key" in out.lower() else "")
        _status[agent] = (time.monotonic(), info)
        info = dict(info)
    model, effort = DEFAULTS[agent]
    info.update(models=models(agent), efforts=[] if agent in NO_EFFORTS else list(EFFORTS), default_model=model,
                default_effort=effort,
                login=LOGINS[agent].public() if agent in LOGINS else None)
    return info


async def listing(fresh: bool = False) -> list[dict]:
    """Агенты машины: Claude Code, Codex, Gemini CLI — всегда (видно, чего нет), остальные ACP — если установлены."""
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
        if pty is None or (self.agent in ACP and self.agent != "gemini") or self.agent == "pi":
            raise AgentError("На этой машине войдите в терминале: " + {"claude": "claude auth login", "codex": "codex login --device-auth", "pi": "pi → /login"}.get(
                self.agent, (ACP.get(self.agent) or {}).get("login") or self.agent))
        if not force and (await status(self.agent, fresh=True))["logged_in"]:
            raise AgentError("Уже вошли — войти заново можно только с подтверждением: текущий вход сотрётся")
        self.state, self.url, self.code, self.error, self.output = "starting", "", "", "", ""
        self.needs_code = self.agent in ("claude", "gemini")
        self.started = time.time()
        env = dict(os.environ, BROWSER="/bin/false", NO_COLOR="1", TERM="dumb")
        if self.agent == "claude":
            argv = [CLAUDE_CLI, "auth", "login", "--claudeai"]
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
        if self.agent == "gemini":
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
            self.state, self.error = "failed", f"Агент не запустился: {e}"
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
            self.error = "Агент не показал ссылку для входа"
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
                    self.error = "Время входа вышло"
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
                    self.error = (tail[0].strip() if tail else "Вход не завершился")[:300]
            bak = GEMINI_CREDS + ".bak"
            if self.agent == "gemini" and os.path.exists(bak):
                if os.path.exists(GEMINI_CREDS):
                    os.remove(bak)
                else:
                    os.replace(bak, GEMINI_CREDS)           # новый вход не случился — прежний на место
                    _status.pop("gemini", None)
            log.info("вход в %s: %s", self.agent, self.state)

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
        if self.agent == "gemini":
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
        """Claude Code и Gemini CLI: код со страницы входа → в pty процесса входа."""
        code = code.strip()
        if self.agent not in ("claude", "gemini") or self.state != "waiting":
            raise AgentError("Этот вход кода не ждёт")
        if not code or len(code) > 400 or any(ch in code for ch in "\r\n"):
            raise AgentError("Вставьте код со страницы входа")
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
        raise AgentError("нет такого агента")
    return LOGINS[agent]
