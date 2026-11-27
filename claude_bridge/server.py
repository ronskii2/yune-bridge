"""HTTP моста: сайт → сессии Claude Code, Codex, Antigravity и других агентов на этой машине.

Два способа: на ПК (remote.py) — make_app на 127.0.0.1 со случайным портом и токеном, сайт ходит через исходящий
WebSocket моста; на машине, где работает сам сайт, — `python -m claude_bridge.server`, unix-сокет DATA/run/bridge.sock
(папку run/ видит только сайт). Каждый запрос несёт токен X-Bridge-Token (CLAUDE_BRIDGE_TOKEN): сокет могут видеть
процессы с другим uid, поэтому одного сокета мало — без токена мост не отвечает.

  GET  /meta                         модели, уровни; агенты и машина (+ поля расширения, extension.py)
  GET  /agents?fresh=1               агенты: установлен, вошёл, модели (agents.py)
  GET  /agents/{agent}/login         вход в агента: ссылка, код, состояние
  POST /agents/{agent}/login         {action: start | code | cancel, code?}
  GET  /limits?agent=claude|codex    лимиты подписки агента на машине: окна, проценты, сброс (limits.py);
                                     у Claude — и переключатель автосжатия (auto_compact)
  POST /agents/{agent}/update        обновить CLI агента (claude update / npm -g …@latest); идёт его ход — 409 busy
  POST /settings                     {auto_compact: bool, chrome: bool, compact_window: int | "auto"} — настройки моста,
                                     общие на все сессии; compact_window — порог автосжатия в settings.json Claude Code
  GET  /context?project=             стартовый контекст Claude Code в папке: правила, память, скиллы, MCP, база (context.py)
  GET  /security?fresh=1             инвентарь для проверки безопасности: обновления, образы, сокеты (security.py)
  GET  /registry?fresh=1             агенты проектов: реестр .yune/agents.json, вызовы ИИ в коде, расписание (registry.py)
  GET  /projects                     проекты для новой сессии
  GET  /branches?project=            ветки git проекта: {current, branches: [{name, when, current, worktree}]}
  GET  /dirs?path=                   подпапки (окно «Другая папка»)
  POST /folders                      {path} — папку в проекты
  GET  /sessions                     список сессий
  POST /sessions                     {project, agent?, model?, effort?, access?, rules?, branch?, branch_new?} → сессия;
                                     branch — другая или новая (branch_new) ветка: сессия в своём git worktree; rules — права
                                     агента Yune (rules.py), мост проверяет каждое действие
  GET  /sessions/{id}?before=&limit= сессия + события
  POST /sessions/{id}/send           {text, files?: [fid], answer?: {mode, rules, short}, mode?: auto | now | later}
                                     answer — режим ответа на сайте; агент работает — сообщение в очередь (auto)
  POST /sessions/{id}/queue          {qid, op: edit | hold | cancel | now, text?}   отложенное сообщение
  POST /sessions/{id}/resume         {when: now | auto | cancel}   после лимита / ошибки: сразу или когда восстановится
  POST /sessions/{id}/upload?name=   тело — байты файла → {fid, name, mime, size}
  GET  /sessions/{id}/files/{fid}    файл сессии (присланный владельцем или показанный Claude)
  POST /sessions/{id}/answer         {rid, answers: {вопрос: ответ}}
  POST /sessions/{id}/secret         {rid, value} | {rid, cancel: true}
  POST /sessions/{id}/permit         {rid, decision: allow | deny | all, text?}   ответ на просьбу разрешения (режим ask)
  POST /sessions/{id}/stop | compact | delete
  POST /sessions/{id}/snooze         {on: bool}   «Отложить» на сайте — автосжатие такую не трогает
  POST /sessions/{id}/rename         {title, if_title?}   if_title — только если название всё ещё такое (автоназвание)
  POST /sessions/{id}/settings       {model?, effort?, access?: full | ask | read}   access — режим доступа сессии
  POST /sessions/{id}/handoff        {agent, model?, effort?} → новая сессия с выжимкой этой
  GET  /events                       SSE: все события всех сессий
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import signal

from aiohttp import web

from . import agents, context, extension, limits, projects, registry, security
from .sessions import DATA, Bridge, BridgeError, append_prompt, device_note


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _strip_loc(path: str) -> str:
    """Убрать file:// и суффикс номера строки (:12, :12:5, :12-30, #L12-L30)."""
    import re
    from urllib.parse import unquote
    p = path.strip().strip("`'\"")
    if p.lower().startswith("file://"):
        p = unquote(p[7:])
        if p.lower().startswith("localhost/"):
            p = p[len("localhost"):]
    p = re.sub(r"#L\d+(?:-L?\d+)?$", "", p)
    p = re.sub(r":\d+(?::\d+|-\d+)?$", "", p)
    return p


def _find_in_tree(root: str, rel: str, max_depth: int = 6, max_entries: int = 15000) -> str | None:
    """Найти относительный путь или имя файла внутри папки сессии (когда агент дал `app.js` или `src/app.js` без
    верхней подпапки). Обход по уровням с ограничением глубины и числа записей, без .git/node_modules."""
    import collections
    want = os.path.normpath(rel.replace("\\", os.sep).replace("/", os.sep)).strip(os.sep)
    if not want or want in (".", "..") or want.startswith(".." + os.sep):
        return None
    want_low = want.lower()
    suffix = os.sep + want
    suffix_low = os.sep + want_low
    ci_hit = None
    seen = 0
    q: collections.deque[tuple[str, str, int]] = collections.deque([(root, "", 0)])
    while q and seen < max_entries:
        cur, pref, depth = q.popleft()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > max_entries:
                break
            name = entry.name
            if name in projects.ICON_SKIP:
                continue
            r = name if not pref else pref + os.sep + name
            if r == want or r.endswith(suffix):
                return entry.path
            if ci_hit is None and (r.lower() == want_low or r.lower().endswith(suffix_low)):
                ci_hit = entry.path
            if depth < max_depth:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        q.append((entry.path, r, depth + 1))
                except OSError:
                    pass
    return ci_hit


def _resolve_local(cwd: str, path: str) -> str:
    """Найти существующий файл/папку в пределах папки сессии, домашней папки пользователя или временной папки:
    агент часто даёт абсолютный путь в ~/Desktop или /tmp, путь с дублированием имени папки проекта (`bot/…` при cwd
    `…/bot`) или короткий относительный путь внутри проекта."""
    import tempfile
    raw = path.strip()
    if not raw or len(raw) > 4096 or "\0" in raw:
        raise BridgeError("no_path: empty path")
    root = os.path.realpath(cwd)
    home = os.path.realpath(os.path.expanduser("~"))
    allowed = [root, home]
    for tmp in ("/tmp", tempfile.gettempdir()):
        if os.path.exists(tmp):
            rt = os.path.realpath(tmp)
            if rt not in allowed:
                allowed.append(rt)

    def ok(p: str) -> str | None:
        rp = os.path.realpath(p)
        if any(_under(rp, base) for base in allowed) and os.path.exists(rp):
            return rp
        return None

    cleaned = _strip_loc(raw)
    ic_clean = projects.clean_path(cleaned) if cleaned else ""
    variants = []
    for v in (raw, cleaned, ic_clean):
        if v and v not in variants:
            variants.append(v)
    for item in variants:
        expanded = os.path.expanduser(item)
        if os.path.isabs(expanded):
            hit = ok(expanded)
            if hit:
                return hit
            rp = os.path.realpath(expanded)
            if not any(_under(rp, base) for base in allowed):
                raise BridgeError("outside: path is outside allowed folders")
            continue
        # 1) прямо от папки сессии
        hit = ok(os.path.join(root, expanded))
        if hit:
            return hit
        # 2) от предков папки сессии (до 4 уровней внутри домашней: `pkg/app.py` при cwd `…/repo/pkg` или соседний файл в репо)
        anc = os.path.dirname(root)
        for _ in range(4):
            if not anc or anc == os.path.dirname(anc) or not any(_under(anc, base) for base in allowed):
                break
            hit = ok(os.path.join(anc, expanded))
            if hit:
                return hit
            anc = os.path.dirname(anc)
        # 3) от домашней папки (например `Desktop/file.pdf` без `~/`)
        if home != root:
            hit = ok(os.path.join(home, expanded))
            if hit:
                return hit
        # 4) поиск по дереву папки сессии (короткий путь или имя файла без верхних подпапок)
        found = _find_in_tree(root, expanded)
        if found:
            hit = ok(found)
            if hit:
                return hit
    rp = os.path.realpath(os.path.join(root, os.path.expanduser(cleaned or raw)))
    if not any(_under(rp, base) for base in allowed):
        raise BridgeError("outside: path is outside allowed folders")
    raise BridgeError("missing: no such file")


async def open_local(cwd: str, path: str, reveal: bool = False) -> None:
    """Открыть файл или папку из ленты сессии на этой машине — программой по умолчанию (macOS — open, Windows —
    startfile, Linux — xdg-open); reveal — показать в Finder / Проводнике. В пределах папки сессии, домашней папки
    или временной папки. Машина без экрана (сервер) — ошибка: открывать некому."""
    import sys
    full = await asyncio.to_thread(_resolve_local, cwd, path)
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise BridgeError("no_screen: this machine has no screen")
    if sys.platform == "win32":
        if reveal:
            await asyncio.create_subprocess_exec("explorer", "/select," + full)
        else:
            os.startfile(full)  # type: ignore[attr-defined]  # noqa: S606
        return
    if sys.platform == "darwin":
        cmd = ["open", "-R", full] if reveal else ["open", full]
    else:
        cmd = ["xdg-open", os.path.dirname(full) if reveal and os.path.isfile(full) else full]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), 10)
    except asyncio.TimeoutError:
        return                                   # программа открылась и держит процесс — это нормально
    if proc.returncode:
        raise BridgeError((err or b"").decode(errors="replace").strip()[:200] or "didn't open")

log = logging.getLogger("claude_bridge")
SOCKET = os.path.join(DATA, "run", "bridge.sock")
PING_SEC = 20
TRIM_KEY = 5000       # ключ ответа — текст вопроса; сверяет с полным текстом sessions.match_answers


def _json(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


def _error(text: str, status: int = 400) -> web.Response:
    return _json({"ok": False, "error": text}, status)


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text='{"ok": false, "error": "bad json"}', content_type="application/json")
    return body


TOKEN = os.environ.get("CLAUDE_BRIDGE_TOKEN", "")


@web.middleware
async def require_token(request, handler):
    if TOKEN and not hmac.compare_digest((request.headers.get("X-Bridge-Token") or "").encode(), TOKEN.encode()):
        log.warning("request without token: %s %s", request.method, request.path)
        return _json({"ok": False, "error": "forbidden"}, status=403)
    return await handler(request)


def make_app(bridge: Bridge) -> web.Application:
    app = web.Application(client_max_size=60 * 1024 * 1024, middlewares=[require_token])
    r = app.router

    async def meta(request):
        extra = extension.attr("meta")
        listing = await agents.listing()
        claude = next((a for a in listing if a["id"] == "claude"), {})
        return _json({"ok": True, "models": claude.get("models") or [], "efforts": claude.get("efforts") or [],
                      "default_model": claude.get("default_model") or "", "default_effort": claude.get("default_effort") or "",
                      "agents": listing, "machine": agents.machine(), **(extra() if extra else {})})

    async def agent_list(request):
        return _json({"ok": True, "agents": await agents.listing(fresh=request.query.get("fresh") == "1"),
                      "machine": agents.machine()})

    async def agent_update(request):
        agent = request.match_info["agent"]
        # идёт ход этого агента — не трогаем (обновление подменило бы CLI под работающей сессией); сайт повторит позже
        if any((x.row.get("agent") or "claude") == agent and x.status != "idle" for x in bridge.sessions.values()):
            return _json({"ok": False, "error": "busy"}, 409)
        try:
            return _json({"ok": True, **await agents.update(agent)})
        except agents.UpdateError as e:
            return _json({"ok": False, "error": str(e)}, 400)

    async def limit_get(request):
        agent = request.query.get("agent") or "claude"
        if agent not in ("claude", "codex"):
            return _json({"ok": False, "error": "unknown agent"}, 400)
        extra = {"auto_compact": bridge.auto_compact, "chrome": bridge.chrome,
                 "compact_window": agents.claude_compact_window()} if agent == "claude" else {}
        return _json({"ok": True, "limits": await limits.get(agent, fresh=request.query.get("fresh") == "1"), **extra})

    async def context_get(request):
        path = request.query.get("project") or ""
        if not path:
            return _error("project")
        return _json({"ok": True, "context": await asyncio.to_thread(context.get, path, append_prompt())})

    async def settings_set(request):
        body = await _body(request)
        if "auto_compact" in body:
            bridge.set_setting("auto_compact", "1" if body["auto_compact"] else "0")
        if "chrome" in body and bool(body["chrome"]) != bridge.chrome:
            bridge.set_setting("chrome", "1" if body["chrome"] else "0")
            await bridge.restart_idle()
        if "compact_window" in body:            # порог автосжатия Claude Code (его settings.json, autoCompactWindow)
            try:
                await asyncio.to_thread(agents.set_claude_compact_window, body["compact_window"])
            except (ValueError, OSError) as e:
                return _json({"ok": False, "error": str(e)}, 400)
        return _json({"ok": True, "auto_compact": bridge.auto_compact, "chrome": bridge.chrome,
                      "compact_window": agents.claude_compact_window()})

    async def security_get(request):
        return _json({"ok": True, **await security.get(fresh=request.query.get("fresh") == "1")})

    async def registry_get(request):
        return _json({"ok": True, **await registry.get(fresh=request.query.get("fresh") == "1")})

    async def agent_login(request):
        try:
            flow = agents.login(request.match_info["agent"])
            if request.method == "GET":
                return _json({"ok": True, "login": flow.public()})
            body = await _body(request)
            action = body.get("action")
            if action == "start":
                state = await flow.start(force=bool(body.get("force")))
            elif action == "code":
                state = flow.send_code(str(body.get("code") or ""))
            elif action == "cancel":
                await flow.cancel()
                state = flow.public()
            else:
                return _error("unknown action")
        except agents.AgentError as e:
            return _error(str(e))
        return _json({"ok": True, "login": state})

    async def project_branches(request):
        got = await asyncio.to_thread(projects.branches, request.query.get("project") or "")
        if got is None:
            return _error("no such project")
        return _json({"ok": True, **got})

    async def project_list(request):
        return _json({"ok": True, "projects": await asyncio.to_thread(projects.listing)})

    async def project_icon(request):
        got = await asyncio.to_thread(projects.icon, request.query.get("path") or "",
                                      "dark" if request.query.get("theme") == "dark" else None)
        if not got:
            raise web.HTTPNotFound()
        return web.FileResponse(got[0], headers={"Content-Type": got[1]})

    async def project_icon_set(request):
        project = request.query.get("path") or ""
        theme = "dark" if request.query.get("theme") == "dark" else None      # картинка для тёмной темы
        mono = request.query.get("mono") == "1"                               # одноцветный SVG — красит сайт
        try:
            if request.content_type == "application/json":
                body = await _body(request)
                ver = await asyncio.to_thread(projects.set_icon, project, set_name=str(body.get("set") or "") or None,
                                              reset=bool(body.get("reset")), theme=theme)
            else:
                ver = await asyncio.to_thread(projects.set_icon, project, data=await request.read(), theme=theme, mono=mono)
        except ValueError as e:
            return _json({"ok": False, "error": str(e)}, 400)
        return _json({"ok": True, "icon": ver})

    async def dir_list(request):
        try:
            return _json({"ok": True, **await asyncio.to_thread(projects.browse, request.query.get("path"))})
        except ValueError as e:
            return _json({"ok": False, "error": str(e)}, 400)

    async def folder_add(request):
        body = await _body(request)
        try:
            card = await asyncio.to_thread(projects.add_folder, str(body.get("path") or ""))
        except ValueError as e:
            return _json({"ok": False, "error": str(e)}, 400)
        return _json({"ok": True, "project": card})

    async def dir_find(request):
        """Папка по имени (перетащили из Finder — браузер отдаёт только имя и что внутри): Spotlight, иначе обход."""
        body = await _body(request)
        inside = body.get("inside") if isinstance(body.get("inside"), list) else []
        found = await asyncio.to_thread(projects.find, str(body.get("name") or ""), [str(x) for x in inside[:60]])
        return _json({"ok": True, "dirs": found})

    async def outside_list(request):
        return _json({"ok": True, "sessions": await asyncio.to_thread(bridge.outside)})

    async def outside_adopt(request):
        body = await _body(request)
        try:
            session, item = bridge.adopt(str(body.get("id") or ""))
        except BridgeError as e:
            return _error(str(e))
        return _json({"ok": True, "session": session.public(), "first": item.get("first") or "",
                      "named": bool(item.get("title"))})

    async def session_list(request):
        return _json({"ok": True, "sessions": bridge.listing()})

    async def session_create(request):
        body = await _body(request)
        project = str(body.get("project") or "")
        try:
            card = await asyncio.to_thread(projects.resolve, project)
            if not card:
                raise BridgeError("no such project")
            branch, tree = str(body.get("branch") or "").strip() or None, None
            if branch:                          # другая или новая ветка — своя папка worktree; git — не в цикле событий
                try:
                    tree = await asyncio.to_thread(projects.branch_dir, project, branch, bool(body.get("branch_new")))
                except ValueError as e:
                    raise BridgeError(str(e)) from None
            session = bridge.create(project, body.get("model"), body.get("effort"), str(body.get("agent") or "claude"),
                                    intro=device_note(body.get("device")), access=body.get("access"),
                                    rules=body.get("rules") if isinstance(body.get("rules"), dict) else None,
                                    branch=branch, cwd=tree)
        except BridgeError as e:
            return _error(str(e))
        return _json({"ok": True, "session": session.public()})

    def _session(request):
        try:
            return bridge.get(request.match_info["id"])
        except BridgeError as e:
            raise web.HTTPNotFound(text=json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False),
                                   content_type="application/json")

    async def session_get(request):
        session = _session(request)
        try:
            before = int(request.query["before"]) if request.query.get("before") else None
            limit = min(int(request.query.get("limit") or 400), 1000)
        except ValueError:
            return _error("bad query")
        events, more = bridge.events(session.id, before, limit)
        return _json({"ok": True, "session": session.public(), "events": events, "more": more})

    async def action(request):
        session = _session(request)
        name = request.match_info["action"]
        body = await _body(request) if request.can_read_body else {}
        try:
            if name == "send":
                fids = body.get("files") if isinstance(body.get("files"), list) else []
                answer = body.get("answer") if isinstance(body.get("answer"), dict) else None
                mode = body.get("mode") if body.get("mode") in ("now", "later") else "auto"
                await session.send(str(body.get("text") or "")[:50000], [str(f) for f in fids], answer, mode)
            elif name == "queue":
                await session.queue_action(str(body.get("qid") or ""), str(body.get("op") or ""),
                                           str(body.get("text") or "")[:50000] if body.get("text") is not None else None)
            elif name == "resume":
                when = body.get("when")
                if when == "now":
                    await session.resume()
                elif when == "auto":
                    at = None
                    if isinstance(body.get("at"), (int, float)):
                        now = time.time()
                        if now < float(body["at"]) <= now + 14 * 86400:
                            at = float(body["at"])
                    session.schedule_resume(at)
                elif when == "cancel":
                    session.cancel_resume()
                else:
                    return _error("when — now | auto | cancel")
            elif name == "answer":
                answers = body.get("answers")
                if not isinstance(answers, dict):
                    return _error("answers must be an object")
                session.resolve(str(body.get("rid")), "question",
                                {str(k)[:TRIM_KEY]: str(v)[:4000] for k, v in answers.items()})
            elif name == "secret":
                value = None if body.get("cancel") else str(body.get("value") or "")
                if value is not None and not value.strip():
                    return _error("empty key")
                session.resolve(str(body.get("rid")), "secret", value)
            elif name == "permit":
                decision = body.get("decision")
                if decision not in ("allow", "deny", "all"):
                    return _error("decision — allow | deny | all")
                said = " ".join(str(body.get("text") or "").split())[:2000] if decision == "deny" else ""
                session.resolve(str(body.get("rid")), "permission", {"decision": decision, "text": said})
            elif name == "background":         # фоновые команды Claude этой сессии — строка «В фоне» на сайте
                from .sessions import bg_list
                return _json({"ok": True, "items": await bg_list(session.client) if session.client else []})
            elif name == "bgstop":
                from .sessions import bg_kill
                try:
                    pid = int(body.get("pid"))
                except (TypeError, ValueError):
                    return _error("pid must be a number")
                if not session.client or not await bg_kill(session.client, pid):
                    return _error("no such background command in this session")
                session._event({"kind": "note", "code": "bg_killed", "text": str(body.get("cmd") or "")[:200]})
            elif name == "stop":
                await session.stop()
            elif name == "compact":
                await session.compact()
            elif name == "shell":                # «!» в поле сайта — команда владельца в терминале машины
                return _json({"ok": True, "shid": await session.shell(str(body.get("cmd") or "")[:8000])})
            elif name == "shellstop":
                await session.shell_stop(str(body.get("shid") or ""))
            elif name == "snooze":
                bridge.update(session.id, snoozed=1 if body.get("on") else 0)
            elif name == "rename":
                title = " ".join(str(body.get("title") or "").split())[:120]
                if not title:
                    return _error("empty title")
                if body.get("if_title") is None or session.row["title"] == str(body["if_title"]):
                    bridge.update(session.id, title=title)
            elif name == "settings":
                await session.configure(body.get("model"), body.get("effort"))
                if body.get("access"):
                    await session.set_access(str(body["access"]))
            elif name == "delete":
                await bridge.delete(session.id)
                return _json({"ok": True})
            elif name == "export":             # файл с этой машины — сайту (инструменты Yune: медиа и project_file_put)
                raw_path = str(body.get("path") or "").strip()
                if not raw_path:
                    return _error("empty path")
                path = os.path.expanduser(projects.clean_path(raw_path) or raw_path)
                if not os.path.isabs(path):
                    path = os.path.join(session.row["cwd"], path)
                if not os.path.isfile(path):
                    path = await asyncio.to_thread(_resolve_local, session.row["cwd"], raw_path)
                info = await asyncio.to_thread(bridge.store_file, session.id, path)
                info.pop("path", None)
                return _json({"ok": True, **info})
            elif name == "sendfile":           # показать файл владельцу в ленте (stdio MCP send_file для ACP и Codex)
                path = os.path.expanduser(str(body.get("path") or "").strip())
                if not path:
                    return _error("empty path")
                if not os.path.isabs(path):
                    path = os.path.join(session.row["cwd"], path)
                info = await asyncio.to_thread(bridge.store_file, session.id, path)
                info.pop("path", None)
                session._event({"kind": "file", **info, "caption": str(body.get("caption") or "")[:1000]})
                return _json({"ok": True, **info})
            elif name == "open":               # путь из ленты — открыть на этой машине (как двойной клик в Finder)
                await open_local(session.row["cwd"], str(body.get("path") or ""), bool(body.get("reveal")))
            elif name == "handoff":
                fresh = bridge.handoff(session.id, str(body.get("agent") or ""), body.get("model"), body.get("effort"))
                return _json({"ok": True, "session": fresh.public(), "from": session.public()})
            else:
                return _error("unknown action", 404)
        except BridgeError as e:
            return _error(str(e))
        return _json({"ok": True, "session": session.public()})

    async def upload(request):
        session = _session(request)
        data = await request.read()
        if not data:
            return _error("empty file")
        try:
            info = await asyncio.to_thread(bridge.save_file, session.id, request.query.get("name") or "file", data)
        except BridgeError as e:
            return _error(str(e))
        info.pop("path")
        return _json({"ok": True, "file": info})

    async def file_get(request):
        session = _session(request)
        info = bridge.file_info(session.id, request.match_info["fid"])
        if not info:
            raise web.HTTPNotFound()
        return web.FileResponse(info["path"], headers={"Content-Type": info["mime"]})

    async def events(request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await response.prepare(request)
        queue = bridge.subscribe()
        try:
            await response.write(b"data: {\"kind\": \"hello\"}\n\n")
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), PING_SEC)
                except asyncio.TimeoutError:
                    event = {"kind": "ping"}
                await response.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            bridge.unsubscribe(queue)
        return response

    async def health(request):
        return _json({"ok": True, "live": sum(1 for s in bridge.sessions.values() if s.client)})

    r.add_get("/health", health)
    r.add_get("/meta", meta)
    r.add_get("/agents", agent_list)
    r.add_post("/agents/{agent}/update", agent_update)
    r.add_get("/limits", limit_get)
    r.add_post("/settings", settings_set)
    r.add_get("/context", context_get)
    r.add_get("/security", security_get)
    r.add_get("/registry", registry_get)
    r.add_route("*", "/agents/{agent:[a-z]+}/login", agent_login)
    r.add_get("/projects", project_list)
    r.add_get("/branches", project_branches)
    r.add_get("/projects/icon", project_icon)
    r.add_post("/projects/icon", project_icon_set)
    r.add_get("/dirs", dir_list)
    r.add_post("/folders", folder_add)
    r.add_post("/dirs/find", dir_find)
    r.add_get("/outside", outside_list)
    r.add_post("/outside", outside_adopt)
    r.add_get("/sessions", session_list)
    r.add_post("/sessions", session_create)
    r.add_get("/sessions/{id:[0-9a-f]{12}}", session_get)
    r.add_post("/sessions/{id:[0-9a-f]{12}}/upload", upload)
    r.add_get("/sessions/{id:[0-9a-f]{12}}/files/{fid:[0-9a-f]{16}}", file_get)
    r.add_post("/sessions/{id:[0-9a-f]{12}}/{action:[a-z]+}", action)
    r.add_get("/events", events)
    return app


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    bridge = Bridge()
    runner = web.AppRunner(make_app(bridge), handle_signals=False)
    await runner.setup()
    os.makedirs(os.path.dirname(SOCKET), exist_ok=True)
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    site = web.UnixSite(runner, SOCKET)
    await site.start()
    os.chmod(SOCKET, 0o666)         # папка run/ видна только контейнерам сайта; там процессы от root
    from . import sessions as _sessions
    _sessions.LOCAL_BRIDGE = {"socket": SOCKET, "token": TOKEN}
    log.info("listening on %s%s", SOCKET, "" if TOKEN else " — NO TOKEN (CLAUDE_BRIDGE_TOKEN is not set)")
    sweeper = asyncio.create_task(bridge.sweep())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    sweeper.cancel()
    await bridge.shutdown()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
