"""Минимальный stdio MCP-сервер моста:
  1. Без флагов — отдаёт ACP-агентам и Codex инструмент send_file (проксирует в локальный HTTP моста
     POST /sessions/{id}/sendfile по TCP или unix-сокету).
  2. С флагом --proxy — stdio-прокси к удалённому HTTP MCP (Yune /mcp) для ACP-агентов без поддержки
     http MCP (например Antigravity). Только stdlib, параллельные вызовы через ThreadPoolExecutor.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
import socket
import ssl
import sys
import threading
from urllib.parse import urlparse

PROXY_TIMEOUT = 310
_STDOUT_LOCK = threading.Lock()
_EMPTY_LISTS = {"resources/list": {"resources": []}, "prompts/list": {"prompts": []}}

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


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS-соединение на заданный IP (config resolve) с проверкой сертификата по имени хоста (SNI)."""

    def __init__(self, host: str, port: int | None, ip: str, timeout: float = PROXY_TIMEOUT):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _post_mcp(msg: dict) -> dict | None:
    url = (os.environ.get("YUNE_MCP_URL") or "").strip()
    token = (os.environ.get("YUNE_MCP_TOKEN") or "").strip()
    sid = (os.environ.get("YUNE_BRIDGE_SESSION") or "").strip()
    resolve = (os.environ.get("YUNE_MCP_RESOLVE") or "").strip()
    mid = msg.get("id")
    is_notif = "id" not in msg
    if not url:
        return None if is_notif else {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "MCP URL is not configured"}}

    u = urlparse(url)
    host = u.hostname or "127.0.0.1"
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    headers = {
        "Host": u.netloc,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Content-Length": str(len(body)),
        "User-Agent": "yune-bridge/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if sid:
        headers["X-Yune-Session"] = sid

    conn: http.client.HTTPConnection | None = None
    try:
        if u.scheme == "https":
            if resolve:
                conn = _PinnedHTTPS(host, u.port, resolve, timeout=PROXY_TIMEOUT)
            else:
                conn = http.client.HTTPSConnection(host, port=u.port, timeout=PROXY_TIMEOUT,
                                                   context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(resolve or host, port=u.port, timeout=PROXY_TIMEOUT)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as e:
        return None if is_notif else {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": f"MCP request failed: {e}"}}
    finally:
        if conn is not None:
            conn.close()

    if is_notif or resp.status == 202 or not raw.strip():
        return None
    if resp.status >= 400:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": f"MCP HTTP {resp.status}"}}
    try:
        reply = json.loads(raw)
    except ValueError:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32700, "message": "Invalid JSON from MCP server"}}
    # Клиенты MCP часто запрашивают resources/list и prompts/list на старте — пустой список лучше ошибки -32601
    method = msg.get("method")
    if isinstance(reply, dict) and isinstance(reply.get("error"), dict) and reply["error"].get("code") == -32601 and method in _EMPTY_LISTS:
        return {"jsonrpc": "2.0", "id": mid, "result": _EMPTY_LISTS[method]}
    return reply if isinstance(reply, dict) else None


def _write(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _STDOUT_LOCK:
        sys.stdout.write(line)
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


def _main_proxy() -> None:
    def _run(msg: dict) -> None:
        reply = _post_mcp(msg)
        if reply is not None:
            _write(reply)

    with ThreadPoolExecutor(max_workers=8) as pool:
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
            pool.submit(_run, msg)


def main() -> None:
    if "--proxy" in sys.argv[1:]:
        _main_proxy()
        return
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
