"""Мост Yune: сессии Claude Code, Codex, Antigravity и других агентов на этой машине для сайта (remote.py — ПК, server.py — HTTP моста)."""
import os
import ssl


def _ca_bundle() -> None:
    """Python с python.org на macOS: пока не запущен «Install Certificates.command», у OpenSSL нет корневых сертификатов —
    HTTPS к сайту падал CERTIFICATE_VERIFY_FAILED (unable to get local issuer certificate). Своего хранилища нет —
    сертификаты из certifi. Здесь, до импорта aiohttp: он создаёт SSL-контекст при импорте."""
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return
    paths = ssl.get_default_verify_paths()
    if paths.cafile or (paths.capath and os.path.isdir(paths.capath) and os.listdir(paths.capath)):
        return
    try:
        import certifi
    except ImportError:
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()


_ca_bundle()


def _steady_malloc() -> None:
    """glibc на Linux: большие буферы — всегда через mmap и сразу назад системе.

    Файлы сессий идут через туннель целиком (байты → base64 → JSON, remote.py), видео в 20 МБ — около 100 МБ буферов
    на запрос. Динамический порог mmap glibc после первого такого буфера поднимается до 32 МБ, и следующие ложатся в
    кучу, которую процесс системе не возвращает: мост на сервере за 4 дня занял 1,25 ГБ при пустых сессиях (23.11.2026).
    Явный порог выключает его подстройку. Две арены вместо 8 на ядро — потоки to_thread не плодят свои кучи.
    Не Linux или не glibc (macOS, Windows, musl) — ничего не делаем."""
    import sys
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.mallopt(-3, 1024 * 1024)       # M_MMAP_THRESHOLD
        libc.mallopt(-8, 2)                 # M_ARENA_MAX
    except (OSError, AttributeError):
        pass


_steady_malloc()
