"""Мост на ПК не даёт машине уснуть, пока сессия работает (владелец 18.11.2026: уснул ноутбук — Claude Code с телефона
встал). Работает и ждёт ответа — держим; ждёт ответа дольше WAIT_HOLD_SEC — отпускаем (батарея). Всё стихло — машина
засыпает как обычно.

Mac — `caffeinate -i -w <pid моста>` (умрёт вместе с мостом); Linux — `systemd-inhibit` над циклом, который ждёт
процесс моста; Windows — SetThreadExecutionState у потока цикла asyncio. Ноутбук с закрытой крышкой без внешнего
экрана всё равно уснёт — это делает железо, не система. Выключить — "keep_awake": false в ~/.yune-bridge/config.json.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time

log = logging.getLogger("claude_bridge")

WAIT_HOLD_SEC = 30 * 60
TICK_SEC = 60


class Awake:
    def __init__(self) -> None:
        self.on = False
        self.proc: subprocess.Popen | None = None

    def _command(self) -> list[str] | None:
        pid = str(os.getpid())
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            return ["caffeinate", "-i", "-w", pid]
        if sys.platform.startswith("linux") and shutil.which("systemd-inhibit"):
            return ["systemd-inhibit", "--what=sleep:idle", "--who=Yune", "--why=Yune: session is running",
                    "--mode=block", "sh", "-c", f"while kill -0 {pid} 2>/dev/null; do sleep 30; done"]
        return None

    def set(self, want: bool) -> None:
        if want == self.on:
            return
        try:
            if sys.platform == "win32":
                import ctypes
                es_continuous, es_system_required = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | (es_system_required if want else 0))
            elif want:
                cmd = self._command()
                if not cmd:
                    log.info("nothing to keep the machine awake (no caffeinate / systemd-inhibit)")
                else:
                    self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL)
            elif self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                self.proc = None
            self.on = want
            log.info("machine %s", "stays awake while a session runs" if want else "may sleep again")
        except Exception as e:  # noqa: BLE001
            log.warning("keep awake: %s", e)


async def watch(bridge) -> None:
    """Следит за статусами сессий моста (Bridge.publish → {"kind": "status"}) и держит машину бодрой, пока нужно."""
    awake, states = Awake(), {}            # сессия → (статус, с какого времени)
    queue = bridge.subscribe()

    def need() -> bool:
        now = time.monotonic()
        return any(st == "running" or (st == "waiting" and now - at < WAIT_HOLD_SEC) for st, at in states.values())

    try:
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), TICK_SEC)
            except asyncio.TimeoutError:
                ev = None
            if ev and ev.get("kind") == "status" and ev.get("session"):
                sid, st = str(ev["session"]), str(ev.get("status") or "")
                if st in ("running", "waiting"):
                    if states.get(sid, ("",))[0] != st:
                        states[sid] = (st, time.monotonic())
                else:
                    states.pop(sid, None)
            elif ev and ev.get("kind") == "deleted" and ev.get("session"):
                states.pop(str(ev["session"]), None)
            awake.set(need())
    finally:
        bridge.unsubscribe(queue)
        awake.set(False)
