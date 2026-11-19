"""HTTP моста: сайт → сессии Claude Code и Codex на этой машине.

Два способа: на ПК (remote.py) — make_app на 127.0.0.1 со случайным портом и токеном, сайт ходит через исходящий
WebSocket моста; на машине, где работает сам сайт, — `python -m claude_bridge.server`, unix-сокет DATA/run/bridge.sock
(папку run/ видит только сайт). Каждый запрос несёт токен X-Bridge-Token (CLAUDE_BRIDGE_TOKEN): сокет могут видеть
процессы с другим uid, поэтому одного сокета мало — без токена мост не отвечает.

  GET  /meta                         модели, уровни; агенты и машина (+ поля расширения, extension.py)
  GET  /agents?fresh=1               агенты: установлен, вошёл, модели (agents.py)
  GET  /agents/{agent}/login         вход в агента: ссылка, код, состояние
  POST /agents/{agent}/login         {action: start | code | cancel, code?}
  GET  /limits?agent=claude|codex    лимиты подписки агента на машине: окна, проценты, сброс (limits.py)
  GET  /security?fresh=1             инвентарь для проверки безопасности: обновления, образы, сокеты (security.py)
  GET  /registry?fresh=1             агенты проектов: реестр .yune/agents.json, вызовы ИИ в коде, расписание (registry.py)
  GET  /projects                     проекты для новой сессии
  GET  /sessions                     список сессий
  POST /sessions                     {project, agent?, model?, effort?, access?} → сессия
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
  POST /sessions/{id}/stop | delete
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

from . import agents, extension, limits, projects, registry, security
from .sessions import DATA, EFFORTS, MODELS, Bridge, BridgeError, DEFAULT_EFFORT, DEFAULT_MODEL, device_note

log = logging.getLogger("claude_bridge")
SOCKET = os.path.join(DATA, "run", "bridge.sock")
PING_SEC = 20


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
        log.warning("запрос без токена: %s %s", request.method, request.path)
        return _json({"ok": False, "error": "forbidden"}, status=403)
    return await handler(request)


def make_app(bridge: Bridge) -> web.Application:
    app = web.Application(client_max_size=60 * 1024 * 1024, middlewares=[require_token])
    r = app.router

    async def meta(request):
        extra = extension.attr("meta")
        return _json({"ok": True, "models": [{"id": k, "name": v[0]} for k, v in MODELS.items()],
                      "efforts": list(EFFORTS), "default_model": DEFAULT_MODEL, "default_effort": DEFAULT_EFFORT,
                      "agents": await agents.listing(), "machine": agents.machine(), **(extra() if extra else {})})

    async def agent_list(request):
        return _json({"ok": True, "agents": await agents.listing(fresh=request.query.get("fresh") == "1"),
                      "machine": agents.machine()})

    async def limit_get(request):
        agent = request.query.get("agent") or "claude"
        if agent not in ("claude", "codex"):
            return _json({"ok": False, "error": "unknown agent"}, 400)
        return _json({"ok": True, "limits": await limits.get(agent, fresh=request.query.get("fresh") == "1")})

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

    async def project_list(request):
        return _json({"ok": True, "projects": await asyncio.to_thread(projects.listing)})

    async def session_list(request):
        return _json({"ok": True, "sessions": bridge.listing()})

    async def session_create(request):
        body = await _body(request)
        project = str(body.get("project") or "")
        try:
            card = await asyncio.to_thread(projects.resolve, project)
            if not card:
                raise BridgeError("нет такого проекта")
            session = bridge.create(project, body.get("model"), body.get("effort"), str(body.get("agent") or "claude"),
                                    intro=device_note(body.get("device")), access=body.get("access"))
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
                    session.schedule_resume()
                elif when == "cancel":
                    session.cancel_resume()
                else:
                    return _error("when — now | auto | cancel")
            elif name == "answer":
                answers = body.get("answers")
                if not isinstance(answers, dict):
                    return _error("answers — объект")
                session.resolve(str(body.get("rid")), "question",
                                {str(k)[:500]: str(v)[:4000] for k, v in answers.items()})
            elif name == "secret":
                value = None if body.get("cancel") else str(body.get("value") or "")
                if value is not None and not value.strip():
                    return _error("пустой ключ")
                session.resolve(str(body.get("rid")), "secret", value)
            elif name == "permit":
                decision = body.get("decision")
                if decision not in ("allow", "deny", "all"):
                    return _error("decision — allow | deny | all")
                said = " ".join(str(body.get("text") or "").split())[:2000] if decision == "deny" else ""
                session.resolve(str(body.get("rid")), "permission", {"decision": decision, "text": said})
            elif name == "stop":
                await session.stop()
            elif name == "rename":
                title = " ".join(str(body.get("title") or "").split())[:120]
                if not title:
                    return _error("пустое название")
                if body.get("if_title") is None or session.row["title"] == str(body["if_title"]):
                    bridge.update(session.id, title=title)
            elif name == "settings":
                await session.configure(body.get("model"), body.get("effort"))
                if body.get("access"):
                    await session.set_access(str(body["access"]))
            elif name == "delete":
                await bridge.delete(session.id)
                return _json({"ok": True})
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
            return _error("пустой файл")
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
    r.add_get("/limits", limit_get)
    r.add_get("/security", security_get)
    r.add_get("/registry", registry_get)
    r.add_route("*", "/agents/{agent:[a-z]+}/login", agent_login)
    r.add_get("/projects", project_list)
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
    log.info("listening on %s%s", SOCKET, "" if TOKEN else " — БЕЗ ТОКЕНА (CLAUDE_BRIDGE_TOKEN не задан)")
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
