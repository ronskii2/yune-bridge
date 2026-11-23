"""Проекты машины для новой сессии: git-репозитории в папках поиска (+ «вся машина» — домашняя папка).

Папки поиска и отдельные папки — из настроек моста (~/.yune-bridge/config.json: roots, folders; папку, выбранную
на сайте «Другой папкой» — browse, add_folder, — мост дописывает в folders сам), окружения
CLAUDE_BRIDGE_ROOTS / CLAUDE_BRIDGE_FOLDERS (через os.pathsep) или местного расширения (extension.py: ROOTS, EXTRA,
NAMES); ничего нет — домашняя папка. Папка без git — тоже проект (ветки и правок у неё нет).

Карточка: имя, путь, ветка, последний коммит (тема, когда), сколько файлов изменено без коммита и сколько новых
файлов вне git (их бывает сотни — данные, снимки; показываются отдельно и блёкло). Порядок — по свежести
последнего коммита (у папки — по времени правки): над чем работали вчера, то и сверху.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

from . import extension

HOME = os.path.expanduser("~")
SKIP = {"proc", "sys", "dev", "run", "tmp", "var", "usr", "lib", "lib64", "bin", "sbin", "etc", "boot", "snap",
        "lost+found", "mnt", "media", "opt", "srv", "root", "home", ".cache", ".local", ".npm", ".t3", ".claude",
        ".codex", "worktrees", "postgres-data", "backups", "node_modules", "Library", "AppData", "Applications"}

_roots: tuple[str, ...] | None = None
_folders: tuple[str, ...] = ()
_save = None          # сохранить отдельные папки (ПК: в config.json моста); нет — выбранная папка живёт до перезапуска
BROWSE_MAX = 500      # подпапок в одном окне выбора

# Значок проекта (как у T3 Code): favicon, icon, logo, logotype, avatar в корне и обычных папках веб-проектов. Ищем один
# раз и запоминаем путь (~/.yune-bridge/icons.json): найденный не ищем снова, пока файл на месте; не нашли — ещё раз
# через неделю (вдруг добавили). Сайт берёт картинку по /projects/icon — в списке проектов только её версия
ICON_NAMES = ("favicon.svg", "favicon.png", "favicon.ico", "icon.svg", "icon.png", "logo.svg", "logo.png",
              "logotype.svg", "logotype.png", "logo-mark.svg", "logo.webp", "icon.webp", "avatar.svg", "avatar.png",
              "apple-touch-icon.png", "logo.jpg", "avatar.jpg", "app-icon.png")
ICON_DIRS = ("", "public", "static", "assets", "src", "src/assets", "src/app", "src/static", "app", "web", "web/public",
             "web/static", "frontend/public", "client/public", "site", "www", "docs", "images", "img", "resources",
             ".github", "brand")
ICON_MAX = 512 * 1024
ICON_RETRY_SEC = 7 * 86400
ICON_MIME = {".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".webp": "image/webp",
             ".jpg": "image/jpeg"}
_icons: dict | None = None


def configure(roots: list[str] | None = None, folders: list[str] | None = None, save=None) -> None:
    """Папки поиска и отдельные папки (ПК: из config.json моста). Без вызова — окружение, расширение или ~."""
    global _roots, _folders, _save
    _roots = tuple(os.path.expanduser(r) for r in roots) if roots else None
    _folders = tuple(os.path.expanduser(f) for f in folders or [])
    _save = save


def _search_roots() -> tuple[str, ...]:
    if _roots:
        return _roots
    env = os.environ.get("CLAUDE_BRIDGE_ROOTS")
    if env:
        return tuple(os.path.expanduser(r) for r in env.split(os.pathsep) if r)
    return _host_roots() or (HOME,)


def names() -> dict[str, str]:
    """Имена проектов по пути (из расширения); нет имени — имя папки."""
    return extension.attr("NAMES") or {}


def _host_roots() -> tuple[str, ...]:
    return tuple(extension.attr("ROOTS") or ())


def _host_mode() -> bool:
    """Папки поиска — из расширения (своя раскладка машины, а не настройки)."""
    return bool(_host_roots()) and _search_roots() == _host_roots()


def _extra_folders() -> tuple[str, ...]:
    env = os.environ.get("CLAUDE_BRIDGE_FOLDERS")
    return _folders + (tuple(os.path.expanduser(f) for f in env.split(os.pathsep) if f) if env else ())


def home_card() -> dict:
    """«Без проекта» — вся машина: домашняя папка."""
    return {"id": "server", "name": (extension.attr("HOME_NAME") if _host_mode() else None) or "Домашняя папка",
            "path": HOME,
            "branch": None, "subject": "Без проекта — вся машина", "when": None, "dirty": 0}


def _git(path: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _is_git(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


def _icons_file() -> str:
    return os.path.join(os.environ.get("CLAUDE_BRIDGE_DATA") or os.path.join(HOME, ".yune-bridge"), "icons.json")


def _icons_db() -> dict:
    global _icons
    if _icons is None:
        try:
            with open(_icons_file(), encoding="utf-8") as f:
                _icons = json.load(f)
            if not isinstance(_icons, dict):
                _icons = {}
        except (OSError, ValueError):
            _icons = {}
    return _icons


def _icons_save() -> None:
    try:
        os.makedirs(os.path.dirname(_icons_file()), exist_ok=True)
        tmp = _icons_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_icons_db(), f, ensure_ascii=False)
        os.replace(tmp, _icons_file())
    except OSError:
        pass


def _icon_find(path: str) -> str:
    """Первый по ICON_NAMES файл в ICON_DIRS проекта (без учёта регистра) — путь от корня проекта или ""."""
    found: dict[str, str] = {}
    for d in ICON_DIRS:
        try:
            entries = os.listdir(os.path.join(path, d))
        except OSError:
            continue
        for e in entries:
            low = e.lower()
            if low in ICON_NAMES and low not in found:
                full = os.path.join(path, d, e)
                try:
                    if os.path.isfile(full) and 0 < os.path.getsize(full) <= ICON_MAX:
                        found[low] = os.path.join(d, e) if d else e
                except OSError:
                    pass
    return next((found[n] for n in ICON_NAMES if n in found), "")


def icon_file(path: str) -> str:
    """Значок проекта — полный путь к файлу или "" (из памяти; ищем, только если не искали или не нашли неделю назад)."""
    db = _icons_db()
    got = db.get(path)
    if isinstance(got, dict):
        rel = got.get("file") or ""
        if rel and os.path.isfile(os.path.join(path, rel)):
            return os.path.join(path, rel)
        if not rel and time.time() - float(got.get("at") or 0) < ICON_RETRY_SEC:
            return ""
    rel = _icon_find(path)
    db[path] = {"file": rel, "at": int(time.time())}
    _icons_save()
    return os.path.join(path, rel) if rel else ""


def _icon_version(path: str) -> str | None:
    full = icon_file(path)
    if not full:
        return None
    try:
        st = os.stat(full)
    except OSError:
        return None
    return f"{int(st.st_mtime)}-{st.st_size}"


def icon(project: str) -> tuple[str, str] | None:
    """Значок проекта из списка (чужой путь не отдаём) → (файл, mime) или None."""
    if project not in candidates():
        return None
    full = icon_file(project)
    mime = ICON_MIME.get(os.path.splitext(full)[1].lower()) if full else None
    return (full, mime) if mime else None


def _card(path: str) -> dict:
    card = _card_info(path)
    card["icon"] = _icon_version(path)
    return card


def _card_info(path: str) -> dict:
    name = names().get(path) or os.path.basename(path.rstrip("/\\")) or path
    if not _is_git(path):                       # папка без git: ни ветки, ни правок — время последней правки
        try:
            when = int(os.path.getmtime(path))
        except OSError:
            when = None
        return {"id": path, "name": name, "path": path, "branch": None, "subject": "", "when": when, "dirty": 0,
                "untracked": 0, "folder": True}
    log = _git(path, "log", "-1", "--format=%ct%x00%s")
    when, _, subject = log.partition("\x00")
    status = _git(path, "status", "--porcelain")
    lines = [line for line in status.splitlines() if line.strip()]
    return {"id": path, "name": name, "path": path, "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD") or None,
            "subject": subject[:140], "when": int(when) if when.isdigit() else None,
            "dirty": sum(1 for line in lines if not line.startswith("??")),       # изменены, но не закоммичены
            "untracked": sum(1 for line in lines if line.startswith("??"))}       # новые файлы, git о них не знает


def candidates() -> list[str]:
    found = []
    for root in _search_roots():
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for name in entries:
            path = os.path.join(root, name)
            if name in SKIP or name.startswith(".") or not os.path.isdir(path):
                continue
            real = os.path.realpath(path)
            if _is_git(real) and real not in found:
                found.append(real)
    if _host_mode():
        for path in extension.attr("EXTRA") or ():
            if _is_git(path) and path not in found:
                found.append(path)
    for folder in _extra_folders():
        real = os.path.realpath(folder)
        if os.path.isdir(real) and real not in found:
            found.append(real)
    return found


def listing() -> list[dict]:
    cards = [_card(p) for p in candidates()]
    cards.sort(key=lambda c: c["when"] or 0, reverse=True)
    return cards + [home_card()]


def browse(path: str | None = None) -> dict:
    """Окно «Другая папка»: подпапки path (по умолчанию — домашняя), без скрытых; git — у репозитория.
    Нет такой папки — ValueError("not_found"), нет доступа (macOS: «Рабочий стол», «Документы» без разрешения) —
    ValueError("no_access")."""
    path = os.path.realpath(os.path.expanduser(path or HOME))
    if not os.path.isdir(path):
        raise ValueError("not_found")
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
    except PermissionError:
        raise ValueError("no_access") from None
    except OSError:
        raise ValueError("not_found") from None
    dirs = []
    for e in entries:
        try:
            if e.name.startswith(".") or not e.is_dir():
                continue
        except OSError:
            continue
        dirs.append({"name": e.name, "path": e.path, "git": _is_git(e.path)})
        if len(dirs) >= BROWSE_MAX:
            break
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else None, "home": HOME, "dirs": dirs}


def add_folder(path: str) -> dict:
    """Выбранная папка — в проекты (отдельные папки, folders): карточка проекта. Домашняя — «вся машина»."""
    global _folders
    real = os.path.realpath(os.path.expanduser(path or ""))
    if not path or not os.path.isdir(real):
        raise ValueError("not_found")
    if real == os.path.realpath(HOME):
        return home_card()
    if real not in candidates():
        _folders = _folders + (real,)
        if _save:
            _save(list(_folders))
    return _card(real)


def resolve(project: str) -> dict | None:
    """id проекта из карточки → карточка; чужой путь не принимаем (только из списка)."""
    if project == "server":
        return home_card()
    return _card(project) if project in candidates() else None
