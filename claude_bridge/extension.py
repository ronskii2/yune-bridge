"""Местное расширение моста — необязательно. Нет его — мост работает как обычно, просто без того, что оно даёт.

Расширение — модуль claude_bridge.host: то, что знает только своя машина (свои папки проектов, свои тулы для Claude).
В архив моста и в открытый репозиторий оно не входит. Где мост его ищет:
  - рядом с модулями пакета (claude_bridge/host.py);
  - в папке из настроек ПК (config.json: "host_dir") — папка добавляется в пакет последней, её модули подхватываются,
    только если в мосте таких нет (так расширение переживает самообновление: оно лежит вне папки моста).

Что модуль может дать (всё необязательно):
  ROOTS: tuple[str, ...]        папки поиска проектов, когда в настройках их нет
  EXTRA: tuple[str, ...]        проекты сверх найденных (только при папках ROOTS)
  NAMES: dict[str, str]         имена проектов по пути
  HOME_NAME: str                подпись карточки «без проекта» при папках ROOTS
  VIEWS: dict[str, list[dict]]  виды папки — несколько строк одного проекта: {путь: [{key, name, icon, note}]}
  tools(session) -> list        тулы Claude (SDK MCP, в сервере рядом с send_file)
  READ_TOOLS: tuple[str, ...]   их имена (mcp__lozhka__…), которым не нужно разрешение в режиме «Спрашивать»
  prompt() -> str               дополнение системной подсказки Claude (о том, что дают эти тулы)
  meta() -> dict                поля к ответу /meta
  AI_HTTP: tuple[tuple, ...]    свои шлюзы моделей для скана «Агентов» (registry.py): (имя, шаблон, подстрока)
"""
from __future__ import annotations

import importlib
import logging
import os

log = logging.getLogger("claude_bridge")

_mod = None
_tried = False


def load(host_dir: str | None = None):
    """Найти расширение (один раз; host_dir — папка из настроек ПК). Возвращает модуль или None."""
    global _mod, _tried
    if host_dir:
        import claude_bridge
        path = os.path.realpath(os.path.expanduser(host_dir))
        if path not in claude_bridge.__path__:
            claude_bridge.__path__.append(path)
        _tried = False
    if _tried:
        return _mod
    _tried = True
    try:
        _mod = importlib.import_module("claude_bridge.host")
    except ModuleNotFoundError as e:
        if e.name != "claude_bridge.host":
            log.exception("bridge extension failed to load")
        _mod = None
    except Exception:  # noqa: BLE001 — сломанное расширение не должно ронять мост
        log.exception("bridge extension failed to load")
        _mod = None
    if _mod is not None:
        log.info("bridge extension: %s", getattr(_mod, "__file__", "?"))
    return _mod


def get():
    return load()


def attr(name: str, default=None):
    return getattr(get(), name, default)
