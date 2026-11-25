"""Мост на ПК владельца (Mac, Windows, Linux) — «Подключения» Yune.

Сайт на своей машине ходит к мосту по unix-сокету (server.py). До ПК так не дотянуться — поэтому мост на ПК сам открывает исходящее
соединение к Yune (WebSocket /api/connections/link) и держит его; сайт шлёт по нему те же запросы, что серверу по
сокету, мост отвечает и отдаёт свои события. Внутри — тот же мост: HTTP-приложение server.make_app на 127.0.0.1
со случайным портом и токеном, туннель только пересылает.

  python -m claude_bridge.remote pair https://yune.jp [--name "MacBook"]   привязать: код → подтвердить в «Подключениях»
  python -m claude_bridge.remote run                                      работать (переподключается сам)

Настройки и ключ машины — ~/.yune-bridge/config.json (права 600): site, machine_id, token, name, roots (папки, где
искать git-проекты; по умолчанию ~/code, ~/projects, ~/Documents, ~), folders (отдельные папки без git), resolve —
адрес, по которому ходить к сайту вместо DNS (как curl --resolve): мост на том же сервере, что и сайт, ставит
"127.0.0.1" и идёт к веб-серверу напрямую, без круга через CDN; сертификат проверяется по имени сайта, как обычно;
host_dir — папка местного расширения (extension.py), необязательно; keep_awake — не давать машине уснуть, пока идёт
сессия (по умолчанию да). Сессии и файлы — там же (bridge.db, files/). Ключ машины сайт хранит хешем; «Отключить
машину» на сайте его отзывает — мост получает 401 и останавливается. Самообновление и проверка сборки — update.py.

Протокол (текстовые кадры JSON):
  сайт → мост  {"t": "req", "id", "method", "path", "json"?, "b64"?}
  мост → сайт  {"t": "res", "id", "status", "json"} | {"t": "res", "id", "status", "b64", "mime"}   (файл)
               {"t": "ev", "ev": {...}}  — события моста, как /events у сервера
               {"t": "hello", "machine": {...}, "version": 1, "build", "caps": ["update", "github"]}
               {"t": "update", "state": "waiting" | "failed", "error"?}   ход самообновления (update.py)
  сайт → мост  {"t": "update", "build", "source"?: "github"}  — сборка моста на сайте другая: обновиться, когда сессии
               затихнут (github — взять релиз открытого репозитория с этим тегом и сверить хеш)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import platform
import secrets
import socket
import sys

import aiohttp
import aiohttp.abc
from aiohttp import web

log = logging.getLogger("claude_bridge")

CONFIG_DIR = os.environ.get("CLAUDE_BRIDGE_DATA") or os.path.expanduser("~/.yune-bridge")
CONFIG = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_ROOTS = ["~/code", "~/projects", "~/Projects", "~/Documents", "~/dev", "~"]
HEADERS = {"X-Lozhka": "1"}             # сайт пускает POST только с этим заголовком (защита от CSRF)
MAX_FRAME = 80 * 1024 * 1024


def load() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)


# ── привязка ─────────────────────────────────────────────────────────────────────────────────────────────────

PENDING = os.path.join(CONFIG_DIR, "pending.json")      # код показан, ввода ждёт запущенный мост (pair --no-wait)

# Что видит человек в терминале — на его языке: YUNE_LANG (ставит установщик: язык сайта, с которого он скачан), язык,
# сохранённый при привязке (config.json «lang»), системная локаль; иначе английский
TEXT = {
    "no_code": {"ja": "サイトからコードを取得できませんでした: {error}", "en": "The site did not give a code: {error}",
                "ru": "Сайт не дал код: {error}"},
    "code": {"ja": "\n接続コード: {code}\nYune の「接続」→「マシンを接続」を開き、このコードを入力してください（有効期限 10 分）。\n",
             "en": "\nPairing code: {code}\nOpen Yune → Connections → Connect a machine and enter this code (valid for 10 minutes).\n",
             "ru": "\nКод привязки: {code}\nОткройте Yune → «Подключения» → «Подключить машину» и введите этот код (действует 10 минут).\n"},
    "paired": {"ja": "完了: 「{name}」を接続しました。起動: python -m claude_bridge.remote run",
               "en": "Done: “{name}” is connected. Start it: python -m claude_bridge.remote run",
               "ru": "Готово: «{name}» привязана. Запустите: python -m claude_bridge.remote run"},
    "expired": {"ja": "コードの有効期限が切れたか、拒否されました。もう一度接続してください",
                "en": "The code expired or was declined — run pairing again",
                "ru": "Код истёк или отклонён — запустите привязку заново"},
    "not_paired": {"ja": "このマシンはまだ接続されていません: python -m claude_bridge.remote pair <Yune のアドレス>",
                   "en": "This machine is not paired yet: python -m claude_bridge.remote pair <Yune address>",
                   "ru": "Машина не привязана: python -m claude_bridge.remote pair <адрес Yune>"},
    "computer": {"ja": "パソコン", "en": "Computer", "ru": "Компьютер"},
}


def lang() -> str:
    for value in (os.environ.get("YUNE_LANG"), load().get("lang") if os.path.exists(CONFIG) else None,
                  os.environ.get("LC_ALL"), os.environ.get("LANG")):
        code = (value or "").strip().lower()[:2]
        if code in ("ja", "en", "ru"):
            return code
    return "en"


def say(key: str, **kw) -> str:
    return TEXT[key][lang()].format(**kw)


async def pair(site: str, name: str, wait: bool = True) -> None:
    """Код привязки. wait — ждать ввода здесь (ручная установка). Без ожидания (установщик) — код печатается, команда сразу
    завершается, а ввода ждёт мост (run): агент на машине (Claude Code, Codex), запустивший установщик, видит вывод только
    после конца команды и обрывает долгие команды — код до человека бы не дошёл."""
    from . import agents
    site = site.rstrip("/")
    info = {**agents.machine(), "name": name}
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        async with s.post(f"{site}/api/connections/pair", json=info) as resp:
            data = await resp.json(content_type=None)
        if not data.get("ok"):
            sys.exit(say("no_code", error=data.get("error") or resp.status))
        print(say("code", code=data["code"]), flush=True)
        pending = {"site": site, "pair_id": data["pair_id"], "secret": data["secret"], "name": name, "lang": lang()}
        if not wait:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(PENDING, "w", encoding="utf-8") as f:
                json.dump(pending, f)
            if os.name != "nt":
                os.chmod(PENDING, 0o600)
            return
        if not await await_pair(s, pending):
            sys.exit(say("expired"))
        print(say("paired", name=name))


async def await_pair(s: aiohttp.ClientSession, pending: dict) -> bool:
    """Ждать, пока человек введёт код на сайте; ввёл — ключ машины в config.json. False — код истёк или отклонён."""
    while True:
        await asyncio.sleep(3)
        try:          # сайт перезапускается или прокси вернул страницу ошибки — просто спросим ещё раз
            async with s.post(f"{pending['site']}/api/connections/pair/poll",
                              json={"pair_id": pending["pair_id"], "secret": pending["secret"]}) as resp:
                state = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            continue
        if state.get("state") == "done":
            cfg = load()
            cfg.update(site=pending["site"], machine_id=state["machine_id"], token=state["token"], name=pending["name"],
                       lang=pending.get("lang") or lang())
            cfg.setdefault("roots", DEFAULT_ROOTS)
            cfg.setdefault("folders", [])
            save(cfg)
            return True
        if state.get("state") != "pending":
            return False


async def finish_pending() -> bool:
    """Мост запущен, а машина ещё не привязана: дождаться кода, показанного установщиком (pending.json)."""
    try:
        with open(PENDING, encoding="utf-8") as f:
            pending = json.load(f)
    except (OSError, ValueError):
        return False
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        ok = await await_pair(s, pending)
    try:          # установщик могли запустить заново — тогда в файле уже новый код, его не трогаем
        with open(PENDING, encoding="utf-8") as f:
            if json.load(f).get("pair_id") == pending["pair_id"]:
                os.remove(PENDING)
    except (OSError, ValueError):
        pass
    return ok


# ── работа: локальный мост + туннель ─────────────────────────────────────────────────────────────────────────

class Pinned(aiohttp.abc.AbstractResolver):
    """Имя сайта → заданный адрес (config resolve), остальное — обычным DNS."""
    def __init__(self, host: str, ip: str):
        self.host, self.ip, self.dns = host, ip, aiohttp.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host != self.host:
            return await self.dns.resolve(host, port, family)
        return [{"hostname": host, "host": self.ip, "port": port, "family": socket.AF_INET, "proto": 0,
                 "flags": socket.AI_NUMERICHOST}]

    async def close(self):
        await self.dns.close()


def connector(cfg: dict):
    from urllib.parse import urlparse
    if not cfg.get("resolve"):
        return None
    return aiohttp.TCPConnector(resolver=Pinned(urlparse(cfg["site"]).hostname, cfg["resolve"]))


class Tunnel:
    def _keep_folders(self, folders: list[str]) -> None:
        """Папка, выбранная на сайте, — в config.json: после перезапуска она снова в проектах."""
        self.cfg["folders"] = folders
        cfg = load()
        cfg["folders"] = folders
        save(cfg)

    def __init__(self, cfg: dict):
        from . import extension, projects
        extension.load(cfg.get("host_dir"))
        projects.configure(cfg.get("roots") or DEFAULT_ROOTS, cfg.get("folders") or [], save=self._keep_folders)
        from . import server, sessions
        from .sessions import Bridge
        # MCP Yune в сессиях этой машины — ключом машины (сайт сверяет его так же, как у WebSocket)
        sessions.YUNE_MCP = {"url": cfg["site"].rstrip("/") + "/mcp", "token": f"m.{cfg['machine_id']}.{cfg['token']}"}
        self.cfg = cfg
        self.server = server
        self.bridge = Bridge(CONFIG_DIR)
        self.local_token = secrets.token_hex(24)
        server.TOKEN = self.local_token       # локальный HTTP моста — только с этим токеном
        self.base = ""
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.stop = asyncio.Event()
        self.updating: asyncio.Task | None = None
        self.restart = False                  # обновились — main() перезапустит мост на месте (update.restart)

    async def start_local(self) -> web.AppRunner:
        runner = web.AppRunner(self.server.make_app(self.bridge), handle_signals=False)
        await runner.setup()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        site = web.SockSite(runner, sock)
        await site.start()
        self.base = f"http://127.0.0.1:{port}"
        log.info("local bridge: %s", self.base)
        return runner

    async def run(self) -> None:
        runner = await self.start_local()
        sweeper = asyncio.create_task(self.bridge.sweep())
        from . import awake          # пока сессия работает — машина не засыпает (выключить: "keep_awake": false)
        keeper = asyncio.create_task(awake.watch(self.bridge)) if self.cfg.get("keep_awake", True) else None
        delay = 2
        url = self.cfg["site"].replace("https://", "wss://").replace("http://", "ws://") + "/api/connections/link"
        headers = {"Authorization": f"Bearer {self.cfg['token']}", "X-Machine": self.cfg["machine_id"]}
        try:
            async with aiohttp.ClientSession(headers={"X-Bridge-Token": self.local_token}) as local:
                while not self.stop.is_set():
                    try:
                        async with aiohttp.ClientSession(connector=connector(self.cfg)) as s:
                            # сжатие кадров (permessage-deflate): лента сессии — JSON с выводом команд, ужимается в 3–4 раза
                            async with s.ws_connect(url, headers=headers, heartbeat=25, max_msg_size=MAX_FRAME,
                                                    compress=15) as ws:
                                self.ws = ws
                                delay = 2
                                from . import agents, update
                                await ws.send_json({"t": "hello", "machine": {**agents.machine(), "name": self.cfg.get("name")},
                                                    "version": 1, "build": update.BUILD,
                                                    "caps": [] if update.managed() else update.CAPS})
                                log.info("connected to %s", self.cfg["site"])
                                events = asyncio.create_task(self.forward_events(ws))
                                try:
                                    async for msg in ws:
                                        if msg.type == aiohttp.WSMsgType.TEXT:
                                            req = json.loads(msg.data)
                                            if req.get("t") == "req":
                                                asyncio.create_task(self.handle(local, ws, req))
                                            elif req.get("t") == "update" and not self.updating:
                                                self.updating = asyncio.create_task(self.self_update(
                                                    str(req.get("build") or ""), str(req.get("source") or "")))
                                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                            break
                                finally:
                                    events.cancel()
                                    self.ws = None
                    except aiohttp.WSServerHandshakeError as e:
                        if e.status in (401, 403):
                            log.error("the site refuses this machine (%s): it was disconnected — pair it again (pair)", e.status)
                            return
                        log.warning("the site answered %s — retry in %s s", e.status, delay)
                    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                        log.warning("no connection to the site: %s — retry in %s s", e, delay)
                    try:
                        await asyncio.wait_for(self.stop.wait(), delay)
                    except asyncio.TimeoutError:
                        pass
                    delay = min(delay * 2, 60)
        finally:
            sweeper.cancel()
            if keeper:
                keeper.cancel()
            await self.bridge.shutdown()
            await runner.cleanup()

    async def self_update(self, build: str, source: str = "") -> None:
        """Команда сайта: дождаться, пока сессии затихнут, поставить новую сборку и перезапуститься (update.py).
        Источник закреплён на GitHub (update.pinned) — берём релиз оттуда, что бы ни сказал сайт."""
        from . import update
        try:
            if not build or build == update.BUILD or update.managed():
                return
            if update.pinned() == "github" and source != "github":
                log.warning("the site offers build %s not from GitHub — source is pinned, taking the GitHub release", build)
                source = "github"
            if any(getattr(x, "status", "idle") != "idle" for x in list(self.bridge.sessions.values())):
                await self.send_status("waiting")
                log.info("bridge update to %s — after sessions end", build)
                await update.wait_idle(self.bridge)
            log.info("updating bridge %s → %s (%s)", update.BUILD or "—", build, source or "site")
            if source == "github":                # к GitHub — без заголовков и адреса сайта
                async with aiohttp.ClientSession() as s:
                    files = await update.fetch(self.cfg["site"], s, build, "github")
            else:
                async with aiohttp.ClientSession(connector=connector(self.cfg), headers=HEADERS) as s:
                    files = await update.fetch(self.cfg["site"], s, build, "site")
            await update.install(files)
            if source == "github":
                update.pin("github")
            self.restart = True
            self.stop.set()
            if self.ws is not None:
                await self.ws.close()
        except Exception as e:  # noqa: BLE001 — старый мост цел и работает дальше; сайт покажет ошибку
            log.exception("bridge update")
            await self.send_status("failed", str(e)[:300])
        finally:
            self.updating = None

    async def send_status(self, state: str, error: str = "") -> None:
        if self.ws is not None:
            try:
                await self.ws.send_json({"t": "update", "state": state, **({"error": error} if error else {})})
            except (ConnectionResetError, RuntimeError, aiohttp.ClientError):
                pass

    async def forward_events(self, ws) -> None:
        queue = self.bridge.subscribe()
        try:
            await ws.send_json({"t": "ev", "ev": {"kind": "resync"}})
            while True:
                event = await queue.get()
                await ws.send_str(json.dumps({"t": "ev", "ev": event}, ensure_ascii=False))
        except (ConnectionResetError, aiohttp.ClientError, RuntimeError):
            pass
        finally:
            self.bridge.unsubscribe(queue)

    async def handle(self, local: aiohttp.ClientSession, ws, req: dict) -> None:
        """Запрос сайта → локальный HTTP моста → ответ в туннель (JSON или файл base64)."""
        rid, method, path = req.get("id"), str(req.get("method") or "GET"), str(req.get("path") or "/")
        if not path.startswith("/") or path.startswith("/events"):
            await ws.send_json({"t": "res", "id": rid, "status": 400, "json": {"ok": False, "error": "bad path"}})
            return
        kwargs = {}
        if req.get("b64") is not None:
            kwargs["data"] = base64.b64decode(req["b64"])
        elif req.get("json") is not None:
            kwargs["json"] = req["json"]
        try:
            async with local.request(method, self.base + path, timeout=aiohttp.ClientTimeout(total=300), **kwargs) as resp:
                ctype = resp.headers.get("Content-Type", "")
                if "application/json" in ctype:
                    out = {"t": "res", "id": rid, "status": resp.status, "json": await resp.json(content_type=None)}
                else:
                    out = {"t": "res", "id": rid, "status": resp.status, "mime": ctype,
                           "b64": base64.b64encode(await resp.read()).decode()}
        except Exception as e:  # noqa: BLE001
            out = {"t": "res", "id": rid, "status": 502, "json": {"ok": False, "error": f"bridge: {e}"[:300]}}
        try:
            await ws.send_str(json.dumps(out, ensure_ascii=False))
        except (ConnectionResetError, RuntimeError):
            pass


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m claude_bridge.remote", description="Yune bridge on this machine")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("pair", help="connect this machine to Yune")
    pp.add_argument("site", help="Yune address, e.g. https://yune.jp")
    pp.add_argument("--name", default=platform.node() or say("computer"), help="machine name shown in Yune")
    pp.add_argument("--no-wait", action="store_true")   # показать код и выйти; ввода ждёт мост (run) — так зовёт установщик
    sub.add_parser("run", help="run (keeps the connection to Yune)")
    args = p.parse_args()
    if args.cmd == "pair":
        asyncio.run(pair(args.site, args.name, wait=not args.no_wait))
        return
    cfg = load()
    if not cfg.get("token") and os.path.exists(PENDING):
        if not asyncio.run(finish_pending()):
            sys.exit(say("expired"))
        cfg = load()
    if not cfg.get("token"):
        sys.exit(say("not_paired"))
    tunnel = Tunnel(cfg)
    asyncio.run(tunnel.run())
    if tunnel.restart:
        from . import update
        update.restart()


if __name__ == "__main__":
    main()
