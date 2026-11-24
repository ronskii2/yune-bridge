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

import collections
import hashlib
import json
import os
import re
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

# Значок проекта (как у T3 Code): favicon, apple-touch-icon, logo, icon, avatar — по всему проекту до глубины ICON_DEPTH
# (без node_modules, .git, сборок): у веб-проектов он лежит в public/, static/, assets/… Ищем один раз и
# запоминаем путь (~/.yune-bridge/icons.json): найденный не ищем снова, пока файл на месте; не нашли — ещё раз через
# неделю. Сайт берёт картинку по /projects/icon — в списке проектов только её версия
ICON_KINDS = ("favicon", "apple-touch-icon", "logo", "logotype", "icon", "app-icon", "avatar", "logo-mark", "mark")
ICON_EXT = (".svg", ".png", ".ico", ".webp", ".jpg")
ICON_RE = re.compile(r"^(" + "|".join(re.escape(k) for k in ICON_KINDS) + r")([-_.@][\w.@-]*)?(" +
                     "|".join(re.escape(e) for e in ICON_EXT) + r")$", re.I)
ICON_SKIP = {"node_modules", ".git", ".venv", "venv", "env", "__pycache__", "dist", "build", "out", "target", "vendor",
             "coverage", "worktrees", ".next", ".nuxt", ".cache", "Pods", "DerivedData", "site-packages", "tmp", "logs"}
ICON_DEPTH = 4
ICON_SCAN_MAX = 20000     # записей папок за один поиск (по уровням, мелкие — первыми); большой репозиторий — не дольше
ICON_MAX = 512 * 1024
ICON_RETRY_SEC = 7 * 86400
ICON_CACHE_V = 2          # поменялся способ поиска — прежние «не нашли» ищем заново
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
    """Лучший значок проекта — путь от корня или "". Порядок: вид (favicon, apple-touch-icon, logo…), имя без
    приставки (logo.svg раньше logo-openai.png — чужие логотипы в статьях), мельче глубина, svg/png раньше ico."""
    best, best_key, seen = "", None, 0
    queue = collections.deque([(path, 0)])          # по уровням: у верхних папок значок вероятнее
    while queue:
        d, depth = queue.popleft()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen > ICON_SCAN_MAX:
                return best
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < ICON_DEPTH and e.name not in ICON_SKIP and (not e.name.startswith(".") or e.name == ".github"):
                        queue.append((e.path, depth + 1))
                    continue
                m = ICON_RE.match(e.name)
                if not m or not e.is_file() or not 0 < e.stat().st_size <= ICON_MAX:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(e.path, path)
            key = (ICON_KINDS.index(m.group(1).lower()), bool(m.group(2)), depth, ICON_EXT.index(m.group(3).lower()), len(rel), rel)
            if best_key is None or key < best_key:
                best, best_key = rel, key
    return best


def icon_file(path: str) -> str:
    """Значок проекта — полный путь к файлу или "" (из памяти; ищем, только если не искали или не нашли неделю назад)."""
    db = _icons_db()
    got = db.get(path)
    if isinstance(got, dict) and got.get("v") == ICON_CACHE_V:
        rel = got.get("file") or ""
        if rel and os.path.isfile(os.path.join(path, rel)):
            return os.path.join(path, rel)
        if not rel and time.time() - float(got.get("at") or 0) < ICON_RETRY_SEC:
            return ""
    rel = _icon_find(path)
    db[path] = {"file": rel, "at": int(time.time()), "v": ICON_CACHE_V}
    _icons_save()
    return os.path.join(path, rel) if rel else ""


# ── виды проекта и свой значок ───────────────────────────────────────────────────────────────────────────────
# Вид — та же папка под другим именем (расширение: VIEWS = {путь: [{"key", "name", "icon", "note"}]}): один код,
# несколько витрин. Каждый вид — своя строка в списке (id «путь#key»), свои сессии и значок; note — первой строкой
# агенту в начале сессии (над чем работаем). Свой значок (выбрал человек на сайте) — сильнее найденного: картинка
# (~/.yune-bridge/project-icons/) или значок из набора сайта («i:<имя>» — сайт рисует его сам, по теме)
SET_RE = re.compile(r"^[a-z0-9-]{1,40}$")


def _views() -> dict:
    return extension.attr("VIEWS") or {}


def _split(project: str) -> tuple[str, str]:
    base, _, key = project.partition("#")
    return base, key


def _view(path: str, key: str) -> dict | None:
    return next((v for v in _views().get(path) or () if v.get("key") == key), None) if key else None


def _known(project: str) -> bool:
    base, key = _split(project)
    return base in candidates() and (not key or _view(base, key) is not None)


def _icon_source(project: str) -> tuple[str, str]:
    """Значок проекта (или вида) → ("set", имя) | ("file", полный путь) | ("", "")."""
    base, key = _split(project)
    own = _icons_db().get("@" + project)
    if isinstance(own, dict):
        if own.get("set"):
            return "set", own["set"]
        if own.get("file") and os.path.isfile(own["file"]):
            return "file", own["file"]
    view = _view(base, key)
    if view and view.get("icon") and os.path.isfile(os.path.join(base, view["icon"])):
        return "file", os.path.join(base, view["icon"])
    full = icon_file(base)
    return ("file", full) if full else ("", "")


def _icon_version(project: str) -> str | None:
    kind, value = _icon_source(project)
    if kind == "set":
        return "i:" + value
    if kind != "file":
        return None
    try:
        st = os.stat(value)
    except OSError:
        return None
    return f"{int(st.st_mtime)}-{st.st_size}"


def icon(project: str) -> tuple[str, str] | None:
    """Картинка-значок проекта из списка (чужой путь не отдаём) → (файл, mime) или None."""
    if not _known(project):
        return None
    kind, full = _icon_source(project)
    mime = ICON_MIME.get(os.path.splitext(full)[1].lower()) if kind == "file" else None
    return (full, mime) if mime else None


def _sniff(data: bytes) -> str | None:
    head = data[:2048].lstrip().lower()
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"\x00\x00\x01\x00":
        return ".ico"
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return ".svg"
    return None


def set_icon(project: str, data: bytes | None = None, set_name: str | None = None, reset: bool = False) -> str | None:
    """Свой значок проекта: картинка (data), значок набора сайта (set_name) или reset — снова найденный. → версия."""
    if not _known(project):
        raise ValueError("not_found")
    db, key = _icons_db(), "@" + project
    old = db.get(key) if isinstance(db.get(key), dict) else {}
    store = os.path.join(os.path.dirname(_icons_file()), "project-icons")
    if reset:
        db.pop(key, None)
    elif set_name:
        if not SET_RE.match(set_name):
            raise ValueError("bad_icon")
        db[key] = {"set": set_name}
    elif data:
        ext = _sniff(data)
        if not ext or len(data) > ICON_MAX:
            raise ValueError("bad_icon")
        os.makedirs(store, exist_ok=True)
        name = hashlib.sha1(project.encode()).hexdigest()[:16] + f"-{int(time.time())}{ext}"
        with open(os.path.join(store, name), "wb") as f:
            f.write(data)
        db[key] = {"file": os.path.join(store, name)}
    else:
        raise ValueError("bad_icon")
    was = old.get("file")
    if was and was != (db.get(key) or {}).get("file") and os.path.dirname(was) == store:
        try:
            os.remove(was)
        except OSError:
            pass
    _icons_save()
    return _icon_version(project)


def _card(path: str) -> dict:
    card = _card_info(path)
    card["icon"] = _icon_version(path)
    return card


def _cards(path: str) -> list[dict]:
    """Карточки папки: одна — или по одной на каждый вид (VIEWS)."""
    views = _views().get(path)
    if not views:
        return [_card(path)]
    info, out = _card_info(path), []
    for v in views:
        pid = path + "#" + v["key"]
        out.append({**info, "id": pid, "name": v.get("name") or info["name"], "view": v["key"], "icon": _icon_version(pid)})
    return out


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
    cards = [c for p in candidates() for c in _cards(p)]
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
    if not _known(project):
        return None
    base, key = _split(project)
    if not key:
        return _card(base)
    view = _view(base, key)
    return {**_card_info(base), "id": project, "name": view.get("name") or os.path.basename(base), "view": key,
            "note": view.get("note") or "", "icon": _icon_version(project)}
