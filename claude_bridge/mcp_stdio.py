"""Минимальный stdio MCP-сервер моста: отдаёт ACP-агентам и Codex инструмент send_file.

Запускается агентом как подпроцесс (JSON-RPC 2.0 построчно по stdin/stdout, без сторонних пакетов — только stdlib).
Вызов send_file проксирует в локальный HTTP моста (POST /sessions/{id}/sendfile) — по TCP (remote.py) или unix-сокету
(server.py); мост сохраняет файл в папку сессии и шлёт событие kind: "file" в ленту сайта.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
from urllib.parse import urlparse

TOOL = {
    "name": "send_file",
    "description": (
        "Show the owner a file from this machine in the session feed on the site: images and video right in the feed, "
        "anything else as a download. Up to 50 MB. The file is copied, the original can keep changing."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the file on this machine"},
            "caption": {"type": "string", "description": "Caption for the owner, in their language; may be empty"},
        },
        "required": ["path"],
    },
}


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 60):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


def _post_sendfile(path: str, caption: str) -> tuple[bool, str]:
    sid = os.environ.get("YUNE_BRIDGE_SESSION") or ""
    token = os.environ.get("YUNE_BRIDGE_TOKEN") or ""
    url = os.environ.get("YUNE_BRIDGE_URL") or ""
    sock_path = os.environ.get("YUNE_BRIDGE_SOCK") or ""
    if not sid or not (url or sock_path):
        return False, "File not sent: bridge endpoint is not configured."

    body = json.dumps({"path": path, "caption": caption}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if token:
        headers["X-Bridge-Token"] = token

    conn: http.client.HTTPConnection | None = None
    try:
        if url:
            u = urlparse(url)
            conn = http.client.HTTPConnection(u.hostname or "127.0.0.1", u.port or 80, timeout=60)
        else:
            conn = _UnixHTTP(sock_path, timeout=60)
        conn.request("POST", f"/sessions/{sid}/sendfile", body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
    except OSError as e:
        return False, f"File not sent: {e}"
    finally:
        if conn is not None:
            conn.close()

    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if resp.status != 200 or not data.get("ok"):
        err = data.get("error") or f"HTTP {resp.status}"
        return False, f"File not sent: {err}"
    return True, f"The owner sees {data.get('name') or os.path.basename(path)} in the feed."


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(msg: dict) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method")
    if not method:
        return None
    if mid is None:
        return None  # уведомление — ответа не ждёт

    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        ver = str(params.get("protocolVersion") or "2024-11-05")
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": ver,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "lozhka", "version": "1.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
    if method == "tools/call":
        name = params.get("name")
        if name != "send_file":
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        path = str(args.get("path") or "").strip()
        caption = str(args.get("caption") or "")
        ok, text = _post_sendfile(path, caption)
        res: dict = {"content": [{"type": "text", "text": text}]}
        if not ok:
            res["isError"] = True
        return {"jsonrpc": "2.0", "id": mid, "result": res}

    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        reply = _handle(msg)
        if reply is not None:
            _write(reply)


if __name__ == "__main__":
    main()
