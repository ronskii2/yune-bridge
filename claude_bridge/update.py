"""Сборка моста и самообновление на ПК (remote.py).

Сборка (build) — хеш файлов моста: модули пакета из белого списка PACKAGE и requirements.txt. Один алгоритм на всё:
архив, который отдаёт сайт, релиз открытого репозитория и checkout — у одинаковых файлов одинаковый хеш, его можно
пересчитать самому:

  python -m claude_bridge.update --build            сборка этой папки (checkout или установленный мост)
  python -m claude_bridge.update --check [СБОРКА]   файлы совпадают со сборкой (без аргумента — с build.txt), иначе код 1
  python -m claude_bridge.update --pin github       обновляться только релизами открытого репозитория

Самообновление: сайт при подключении видит сборку моста (hello: build, caps) и, если она не та, шлёт
{"t": "update", "build", "source"?}. Мост дожидается, пока ни одна сессия не работает (перезапуск оборвал бы ход),
качает архив, сам пересчитывает хеш содержимого (не совпал со сборкой — отказ, старый мост цел), раскладывает его поверх
своей папки, ставит зависимости, если они поменялись, и перезапускается на месте (os.execv — та же команда, тот же
автозапуск: systemd, launchd, Планировщик).

Откуда архив: source "github" — релиз открытого репозитория с тегом = сборка (адрес зашит здесь, сайт его не задаёт);
иначе — с сайта (/connect/bridge.zip). После установки с GitHub мост закрепляет источник (файл update-source в папке
настроек): дальше он берёт обновления только с GitHub, что бы ни прислал сайт, — сайт не может подсунуть код, которого
нет в открытом репозитории.

Папка моста внутри git-репозитория (разработка, checkout) — не трогаем: там код ведёт git, а не сайт.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile

log = logging.getLogger("claude_bridge")

PKG = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(PKG)
NAME = "claude_bridge"
# модули моста — всё, что входит в сборку; новый модуль — сюда (иначе его не будет ни в архиве, ни в репозитории)
PACKAGE = ("__init__.py", "acp.py", "acp_session.py", "agents.py", "awake.py", "codex_session.py", "context.py",
           "extension.py", "limits.py", "outside.py", "pi_session.py", "projects.py", "registry.py", "remote.py", "rules.py", "security.py", "server.py", "sessions.py", "update.py")
REQUIREMENTS = "requirements.txt"
REPO = "ronskii2/yune-bridge"
RELEASE_URL = "https://github.com/" + REPO + "/releases/download/{build}/yune-bridge.zip"
BUILD_RE = re.compile(r"^[0-9a-f]{12}$")
CAPS = ["update", "github"]
IDLE_CHECK_SEC = 30
PIP_TIMEOUT_SEC = 900
DATA = os.environ.get("CLAUDE_BRIDGE_DATA") or os.path.expanduser("~/.yune-bridge")
PIN = os.path.join(DATA, "update-source")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


BUILD = _read(os.path.join(PKG, "build.txt"))


# ── сборка: файлы и хеш ──────────────────────────────────────────────────────────────────────────────────────

def bundle(root: str | None = None) -> list[tuple[str, bytes]]:
    """Файлы сборки в раскладке архива: claude_bridge/<модуль> и requirements.txt. root — папка, где лежит пакет
    (по умолчанию — эта). requirements.txt — рядом с пакетом (checkout, установленный мост) или внутри пакета
    (дерево-источник, из которого собирается репозиторий)."""
    root = root or APP
    pkg = os.path.join(root, NAME)
    files = []
    for name in PACKAGE:
        with open(os.path.join(pkg, name), "rb") as f:
            files.append((f"{NAME}/{name}", f.read()))
    req = os.path.join(pkg, REQUIREMENTS)
    if not os.path.exists(req):
        req = os.path.join(root, REQUIREMENTS)
    with open(req, "rb") as f:
        files.append((REQUIREMENTS, f.read()))
    return files


def build_of(files: list[tuple[str, bytes]]) -> str:
    """sha256 по парам (имя в архиве, sha256 содержимого) в порядке имён — первые 12 знаков."""
    h = hashlib.sha256()
    for name, data in sorted(files):
        h.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()[:12]


def archive(files: list[tuple[str, bytes]], build: str) -> bytes:
    """Архив сборки: файлы + claude_bridge/build.txt. Даты в архиве постоянные — у одной сборки один и тот же zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(files) + [(f"{NAME}/build.txt", build.encode())]:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return buf.getvalue()


def unpack(data: bytes) -> tuple[str, list[tuple[str, bytes]]]:
    """Проверить архив: только файлы сборки (пакет без подпапок и requirements.txt), хеш содержимого = build.txt.
    Возвращает (сборка, файлы); не сошлось — исключение."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        files, declared = [], ""
        for info in z.infolist():
            name = info.filename
            if info.is_dir():
                continue
            ok = name == REQUIREMENTS or (name.startswith(NAME + "/") and name.count("/") == 1 and ".." not in name)
            if not ok:
                raise RuntimeError(f"bridge.zip: unexpected file {name[:80]}")
            if name == f"{NAME}/build.txt":
                declared = z.read(info).decode(errors="replace").strip()
            else:
                files.append((name, z.read(info)))
    names = {n for n, _ in files}
    if f"{NAME}/remote.py" not in names or REQUIREMENTS not in names or not declared:
        raise RuntimeError("bridge.zip: no remote.py, requirements.txt or build.txt")
    actual = build_of(files)
    if actual != declared:
        raise RuntimeError(f"bridge.zip: content hash {actual} != build.txt {declared}")
    return actual, files


# ── самообновление ───────────────────────────────────────────────────────────────────────────────────────────

def managed() -> str | None:
    """Почему обновлять нельзя (None — можно): мост из git-репозитория обновляет git, не сайт."""
    d = APP
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return f"git checkout {d}"
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def pinned() -> str:
    """Закреплённый источник обновлений: "github" | "" (какой скажет сайт)."""
    return "github" if _read(PIN) == "github" else ""


def pin(source: str) -> None:
    os.makedirs(DATA, exist_ok=True)
    if source == "github":
        with open(PIN, "w", encoding="utf-8") as f:
            f.write("github\n")
    elif os.path.exists(PIN):
        os.remove(PIN)


async def wait_idle(bridge) -> None:
    """Ждать, пока ни одна сессия не идёт и не ждёт ответа владельца."""
    while any(getattr(s, "status", "idle") != "idle" for s in list(bridge.sessions.values())):
        await asyncio.sleep(IDLE_CHECK_SEC)


async def fetch(site: str, session, build: str, source: str) -> list[tuple[str, bytes]]:
    """Скачать архив сборки build и проверить его. source "github" — релиз открытого репозитория (session — без
    заголовков сайта), иначе — сайт. Хеш содержимого должен совпасть и с build.txt, и с build."""
    if not BUILD_RE.match(build):
        raise RuntimeError(f"bad build {build[:40]!r}")
    url = RELEASE_URL.format(build=build) if source == "github" else site.rstrip("/") + "/connect/bridge.zip"
    async with session.get(url) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{url}: HTTP {resp.status}")
        data = await resp.read()
    actual, files = unpack(data)
    if actual != build:
        raise RuntimeError(f"{source or 'site'}: archive build {actual} != {build}")
    return files


async def install(files: list[tuple[str, bytes]]) -> None:
    """Разложить проверенные файлы сборки поверх папки моста; зависимости — если requirements.txt поменялся.
    Ошибка — исключение, старый мост цел (зависимости ставятся до замены файлов)."""
    tmp = tempfile.mkdtemp(prefix="yune-bridge-")
    try:
        for name, data in files:
            path = os.path.join(tmp, *name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        build = build_of(files)
        with open(os.path.join(tmp, NAME, "build.txt"), "w", encoding="utf-8") as f:
            f.write(build)
        new_req = _read(os.path.join(tmp, REQUIREMENTS))
        if new_req != _read(os.path.join(APP, REQUIREMENTS)):     # сначала зависимости: не встали — файлы моста не тронуты
            shutil.copy2(os.path.join(tmp, REQUIREMENTS), os.path.join(APP, REQUIREMENTS + ".new"))
            await _pip(os.path.join(APP, REQUIREMENTS + ".new"))
            os.replace(os.path.join(APP, REQUIREMENTS + ".new"), os.path.join(APP, REQUIREMENTS))
        src = os.path.join(tmp, NAME)
        fresh = set(os.listdir(src))
        for name in fresh:
            tmp_dst = os.path.join(PKG, name + ".new")
            shutil.copy2(os.path.join(src, name), tmp_dst)
            os.replace(tmp_dst, os.path.join(PKG, name))
        for name in os.listdir(PKG):         # модули, которых в новой сборке нет, — убрать (не подхватились бы старые)
            if name.endswith(".py") and name not in fresh:
                os.remove(os.path.join(PKG, name))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Пакеты — с PyPI без настроек pip человека (чужой индекс в pip.conf, который не открывается, вешал установку на
# повторах); не вышло — ещё раз по его настройкам (своё зеркало, прокси). Как в install.sh / install.ps1
PYPI = ["--isolated", "--index-url", "https://pypi.org/simple", "--retries", "2", "--timeout", "20"]


async def _pip(requirements: str) -> None:
    try:
        await _pip_run(PYPI, requirements)
    except RuntimeError:
        await _pip_run([], requirements)


async def _pip_run(opts: list[str], requirements: str) -> None:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "pip", "install", "-q", *opts, "-r", requirements,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), PIP_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("pip: timeout")
    if proc.returncode:
        raise RuntimeError("pip: " + out.decode(errors="replace").strip()[-300:])


def restart() -> None:
    """Перезапуск на месте: тот же интерпретатор и команда, папка моста — рабочая (python -m claude_bridge.remote).
    Mac и Linux — execv (тот же процесс для systemd и launchd); Windows — новый процесс, этот выходит: execv там не
    дружит с пробелами в пути (C:\\Users\\Имя Фамилия)."""
    os.chdir(APP)
    args = [sys.executable, "-m", "claude_bridge.remote", "run"]
    log.info("мост обновлён — перезапуск")
    if os.name == "nt":
        import subprocess
        subprocess.Popen(args, cwd=APP, close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        os._exit(0)
    os.execv(sys.executable, args)


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m claude_bridge.update", description="Yune bridge build hash")
    p.add_argument("--root", default=APP, help="folder that contains claude_bridge/ (default: this one)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--build", action="store_true", help="print the build hash of the files")
    g.add_argument("--check", nargs="?", const="", metavar="BUILD",
                   help="exit 1 unless the files match BUILD (default: claude_bridge/build.txt)")
    g.add_argument("--pin", choices=["github", "site"], help="where self-update may take new builds from")
    args = p.parse_args()
    if args.pin:
        pin(args.pin)
        print(f"update source: {pinned() or 'site'}")
        return
    actual = build_of(bundle(args.root))
    if args.build:
        print(actual)
        return
    expected = args.check or _read(os.path.join(args.root, NAME, "build.txt"))
    if actual != expected:
        sys.exit(f"build mismatch: files {actual}, expected {expected or '(none)'}")
    print(f"ok {actual}")


if __name__ == "__main__":
    main()
