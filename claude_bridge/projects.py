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

import os
import subprocess

from . import extension

HOME = os.path.expanduser("~")
SKIP = {"proc", "sys", "dev", "run", "tmp", "var", "usr", "lib", "lib64", "bin", "sbin", "etc", "boot", "snap",
        "lost+found", "mnt", "media", "opt", "srv", "root", "home", ".cache", ".local", ".npm", ".t3", ".claude",
        ".codex", "worktrees", "postgres-data", "backups", "node_modules", "Library", "AppData", "Applications"}

_roots: tuple[str, ...] | None = None
_folders: tuple[str, ...] = ()
_save = None          # сохранить отдельные папки (ПК: в config.json моста); нет — выбранная папка живёт до перезапуска
BROWSE_MAX = 500      # подпапок в одном окне выбора


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


def _card(path: str) -> dict:
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
