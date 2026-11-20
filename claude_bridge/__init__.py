"""Мост Yune: сессии Claude Code, Codex и Gemini CLI на этой машине для сайта (remote.py — ПК, server.py — HTTP моста)."""
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
