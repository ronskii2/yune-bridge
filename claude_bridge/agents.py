"""Агенты моста: Claude Code и Codex на этой машине — установлен ли, вошёл ли, модели и вход по ссылке.

Каждый агент — свой CLI в домашней папке пользователя (тот же вход, что в терминале): Claude Code — `~/.local/bin/claude`
(сессии — Claude Agent SDK, sessions.py), Codex — `codex` из npm (сессии — `codex exec --json`, codex_session.py).

Вход — тем же CLI, что и в терминале, но без терминала: процесс входа идёт в псевдотерминале (pty), мост читает из
него ссылку (и код устройства у Codex) и отдаёт сайту. Codex ждёт, пока человек введёт код на странице OpenAI, и сам
завершается; Claude Code после входа на claude.com показывает код, его вставляют на сайте — мост пишет его в pty.
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
STATUS_TTL = 60
LOGIN_TTL = 15 * 60

CLAUDE_MODELS = {"opus": ("Opus 5.5", "claude-opus-5-5"), "sonnet": ("Sonnet 5.5", "claude-sonnet-5-5"),
                 "fable": ("Fable 5.1", "claude-fable-5-1"), "haiku": ("Haiku 5.5", "claude-haiku-5-5")}
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
    out = [{"id": "default", "name": "По умолчанию", "hint": default}]
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


def models(agent: str) -> list[dict]:
    if agent == "codex":
        return codex_models()
    return [{"id": k, "name": v[0], "hint": CLAUDE_HINTS.get(k, "")} for k, v in CLAUDE_MODELS.items()]


DEFAULTS = {"claude": ("opus", "high"), "codex": ("default", "high")}
NAMES = {"claude": "Claude Code", "codex": "Codex"}


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
        cli = CLAUDE_CLI if agent == "claude" else CODEX_CLI
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
        elif info["installed"]:
            code, out = await _run(cli, "login", "status")
            info["logged_in"] = code == 0 and "logged in" in out.lower() and "not logged" not in out.lower()
            info["account"] = ("подписка ChatGPT" if "chatgpt" in out.lower()
                               else "ключ API" if "api key" in out.lower() else "")
        _status[agent] = (time.monotonic(), info)
        info = dict(info)
    model, effort = DEFAULTS[agent]
    info.update(models=models(agent), efforts=list(EFFORTS), default_model=model, default_effort=effort,
                login=LOGINS[agent].public() if agent in LOGINS else None)
    return info


async def listing(fresh: bool = False) -> list[dict]:
    found = list(await asyncio.gather(*(status(a, fresh) for a in NAMES)))
    # OpenCode — позже: в списке, чтобы было видно, что его нет
    found.append({"id": "opencode", "name": "OpenCode", "installed": False, "version": None, "logged_in": False,
                  "account": "", "models": [], "efforts": [], "default_model": None, "default_effort": None, "login": None})
    return found


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
        self.needs_code = False     # Claude Code: код со страницы claude.com вставляют на сайте
        self.error = ""
        self.started = 0.0
        self.proc: asyncio.subprocess.Process | None = None
        self.fd = -1
        self.output = ""
        self.task: asyncio.Task | None = None

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
        if pty is None:
            raise AgentError("На этой машине войдите в терминале: " + ("claude auth login" if self.agent == "claude"
                                                                       else "codex login --device-auth"))
        if not force and (await status(self.agent, fresh=True))["logged_in"]:
            raise AgentError("Уже вошли — войти заново можно только с подтверждением: текущий вход сотрётся")
        self.state, self.url, self.code, self.error, self.output = "starting", "", "", "", ""
        self.needs_code = self.agent == "claude"
        self.started = time.time()
        if self.agent == "claude":
            argv = [CLAUDE_CLI, "auth", "login", "--claudeai"]
        else:
            argv = [CODEX_CLI, "login", "--device-auth"]
        env = dict(os.environ, BROWSER="/bin/false", NO_COLOR="1", TERM="dumb")
        master, slave = pty.openpty()
        try:
            self.proc = await asyncio.create_subprocess_exec(*argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                                                             cwd=HOME, start_new_session=True)
        except OSError as e:
            os.close(master)
            self.state, self.error = "failed", f"Агент не запустился: {e}"
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
                ok = code == 0
                if ok:
                    fresh = await status(self.agent, fresh=True)
                    ok = fresh["logged_in"]
                self.state = "done" if ok else "failed"
                if not ok and not self.error:
                    tail = [ln for ln in ANSI.sub("", self.output).splitlines() if ln.strip()][-1:]
                    self.error = (tail[0].strip() if tail else "Вход не завершился")[:300]
            log.info("вход в %s: %s", self.agent, self.state)

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
        """Claude Code: код со страницы claude.com → в pty процесса входа."""
        code = code.strip()
        if self.agent != "claude" or self.state != "waiting":
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
