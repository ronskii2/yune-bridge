"""Проверка безопасности машины — что стоит и что открыто, для «Подключений» Yune.

Мост только собирает инвентарь, без модели и без прав root; сверку с базами уязвимостей (OSV, CISA KEV, endoflife.date)
делает сайт. Наружу уходят имена пакетов и версии, адреса и порты сокетов, имена процессов — ни содержимого файлов,
ни ключей, ни переменных окружения (кроме версий рантаймов из env образа: PG_MAJOR, PYTHON_VERSION, NODE_VERSION,
NGINX_VERSION).

  GET /security?fresh=1  →  {ok, at, os, updates, reboot, images, listeners, runtimes, errors}

- updates — ожидающие обновления: apt (источник: обновления безопасности Ubuntu/Debian или сторонний репозиторий —
  их unattended-upgrades сам не ставит), dnf, softwareupdate (macOS), Homebrew (только «устарело», без CVE), Windows
  Update (категория «безопасность» / «критические») и winget;
- reboot — /var/run/reboot-required: с какого времени и что обновилось;
- images — запущенные контейнеры Docker, по одному на образ: ОС образа и пакеты в экосистемах OSV (Debian:13,
  Ubuntu:24.04:LTS, Alpine:v3.21, PyPI, npm). Читаем через `docker exec` (cat и find в контейнере): папка Docker
  на хосте закрыта для не-root, а `docker save` пишет на диск весь образ;
- listeners — сокеты на всех адресах (0.0.0.0, ::, *) или публичном: от Интернета их отделяет только файрвол.

Кэш — CACHE_SEC: сайт спрашивает раз в сутки, ручная «Проверить» — с fresh=1.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import platform
import re
import shutil
import time

CACHE_SEC = 3600
CMD_SEC = 120
EXEC_SEC = 90
MAX_PACKAGES = 4000
RUNTIME_ENV = ("PG_MAJOR", "PG_VERSION", "PYTHON_VERSION", "NODE_VERSION", "NGINX_VERSION", "REDIS_VERSION", "MYSQL_VERSION")
# Не наружу: loopback, частные сети, link-local, общий диапазон CGNAT (RFC 6598 — его же берут VPN-сети) и ULA IPv6.
PRIVATE = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                             "fc00::/7", "fe80::/10", "169.254.0.0/16")] + [ipaddress.IPv4Network((0x64400000, 10))]

_cache: tuple[float, dict] | None = None
_lock = asyncio.Lock()


async def get(fresh: bool = False) -> dict:
    global _cache
    async with _lock:                       # сайт и «Проверить» одновременно — один сбор
        if _cache and not fresh and time.time() - _cache[0] < CACHE_SEC:
            return _cache[1]
        errors: list[str] = []
        out = {"at": time.time(), "os": _os_info(), "updates": [], "reboot": {"required": False},
               "images": [], "listeners": [], "runtimes": {}, "errors": errors}
        for key, step in (("updates", _updates), ("reboot", _reboot), ("images", _images),
                          ("listeners", _listeners), ("runtimes", _runtimes)):
            try:
                out[key] = await step()
            except Exception as e:          # один шаг не должен прятать остальные
                errors.append(f"{key}: {type(e).__name__}: {str(e)[:200]}")
        _cache = (time.time(), out)
        return out


async def _run(*cmd: str, timeout: int = CMD_SEC, env: dict | None = None) -> tuple[int, str]:
    if not shutil.which(cmd[0]):
        return 127, ""
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                stdin=asyncio.subprocess.DEVNULL, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, ""
    return proc.returncode, out.decode("utf-8", "replace")


def _os_release(text: str) -> dict:
    data = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"')
    return data


def _os_info() -> dict:
    info = {"system": platform.system(), "arch": platform.machine(), "kernel": platform.release()}
    if info["system"] == "Linux":
        for path in ("/etc/os-release", "/usr/lib/os-release"):
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    rel = _os_release(f.read())
                info.update(id=rel.get("ID", ""), version=rel.get("VERSION_ID", ""), name=rel.get("PRETTY_NAME", ""))
                break
    elif info["system"] == "Darwin":
        info.update(id="macos", version=platform.mac_ver()[0], name="macOS " + platform.mac_ver()[0])
    elif info["system"] == "Windows":
        info.update(id="windows", version=platform.version(), name="Windows " + platform.release())   # version: 10.0.<сборка>
    return info


# ── обновления ОС ────────────────────────────────────────────────────────────

INST = re.compile(r"^Inst (\S+) (?:\[(\S+)\] )?\((\S+) (.+?) \[[^\]]+\]\)")


async def _updates() -> list[dict]:
    system = platform.system()
    if system == "Linux" and shutil.which("apt-get"):
        env = dict(os.environ, LC_ALL="C")
        code, out = await _run("apt-get", "-s", "-o", "Debug::NoLocking=1", "dist-upgrade", env=env)
        items = []
        for line in out.splitlines():
            m = INST.match(line)
            if not m:
                continue
            origin = m.group(4)
            distro = origin.split(":", 1)[0] in ("Ubuntu", "Debian", "Debian-Security", "UbuntuESM", "UbuntuESMApps")
            items.append({"name": m.group(1), "from": m.group(2) or "", "to": m.group(3), "origin": origin,
                          "security": distro and "security" in origin.lower(), "third_party": not distro})
        return items
    if system == "Linux" and shutil.which("dnf"):
        code, out = await _run("dnf", "-q", "updateinfo", "list", "--security", "--available")
        items = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1].endswith("Sec."):     # FEDORA-2026-… Important/Sec. pkg-1.2-1.fc40.x86_64
                items.append({"name": parts[2], "from": "", "to": parts[2], "origin": parts[0], "security": True,
                              "third_party": False})
        return items
    if system == "Darwin":
        items = []
        code, out = await _run("softwareupdate", "-l", timeout=180)
        for m in re.finditer(r"\* Label: (.+)\n\s*Title: ([^,]+), Version: ([^,]+)", out):
            items.append({"name": m.group(2).strip(), "from": "", "to": m.group(3).strip(), "origin": "Apple",
                          "security": True, "third_party": False})
        code, out = await _run("brew", "outdated", "--json=v2", timeout=180)
        if code == 0 and out.strip():
            data = json.loads(out)
            for f in data.get("formulae", []) + data.get("casks", []):
                items.append({"name": f["name"], "from": ",".join(f.get("installed_versions") or [""]),
                              "to": f.get("current_version", ""), "origin": "Homebrew", "security": False,
                              "third_party": True})
        return items
    if system == "Windows":
        return await _windows_updates()
    return []


# PowerShell: обновления Windows Update, которые не стоят (поиск — до пары минут), и сокеты на прослушивании — JSON
PS_WU = ("$s=(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher();"
         "$r=$s.Search(\"IsInstalled=0 and IsHidden=0 and Type='Software'\");"
         "@($r.Updates | ForEach-Object { [pscustomobject]@{t=$_.Title; c=(($_.Categories | ForEach-Object { $_.Name }) -join ',')} })"
         " | ConvertTo-Json -Compress")
PS_LISTEN = ("@(Get-NetTCPConnection -State Listen | ForEach-Object { [pscustomobject]@{a=$_.LocalAddress; p=$_.LocalPort;"
             " n=(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName} }) | ConvertTo-Json -Compress")
PS_REBOOT = ("Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired'")


async def _ps(script: str, timeout: int = CMD_SEC) -> str:
    code, out = await _run("powershell", "-NoProfile", "-NonInteractive", "-Command", script, timeout=timeout)
    return out if code == 0 else ""


def _json_list(text: str) -> list:
    try:
        data = json.loads(text) if text.strip() else []
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


async def _windows_updates() -> list[dict]:
    items = []
    for u in _json_list(await _ps(PS_WU, timeout=300)):
        cats = str(u.get("c") or "")
        items.append({"name": str(u.get("t") or "")[:200], "from": "", "to": "", "origin": "Windows Update",
                      "security": "Security" in cats or "Critical" in cats, "third_party": False})
    # winget: таблица «Имя  Id  Версия  Доступно  Источник»; длинный Id вылезает за столбец — разбираем с конца строки:
    # источник, доступная версия, текущая, Id, остальное — имя
    code, out = await _run("winget", "upgrade", "--include-unknown", "--disable-interactivity",
                           "--accept-source-agreements", timeout=180)
    lines = out.splitlines()
    head = next((i for i, l in enumerate(lines) if set(l.strip()) == {"-"} and len(l.strip()) > 10), None)
    for line in lines[head + 1:] if head is not None else []:
        parts = line.split()
        if len(parts) >= 5 and parts[-1] in ("winget", "msstore"):
            items.append({"name": parts[-4].lower(), "from": parts[-3], "to": parts[-2], "origin": "winget",
                          "security": False, "third_party": True})
    return items


async def _reboot() -> dict:
    if platform.system() == "Windows":
        return {"required": (await _ps(PS_REBOOT, timeout=30)).strip().lower() == "true"}
    flag = "/var/run/reboot-required"
    if not os.path.exists(flag):
        return {"required": False}
    pkgs = []
    if os.path.exists(flag + ".pkgs"):
        with open(flag + ".pkgs", encoding="utf-8") as f:
            pkgs = sorted(set(f.read().split()))
    return {"required": True, "since": os.path.getmtime(flag), "packages": pkgs}


# ── Docker ───────────────────────────────────────────────────────────────────

# Внутри контейнера: os-release, базы пакетов ОС, метаданные Python и npm. Без find (distroless) — только ОС.
PROBE = r"""
for f in /etc/os-release /usr/lib/os-release; do [ -r "$f" ] && { echo '@@os'; cat "$f"; break; }; done
[ -r /var/lib/dpkg/status ] && { echo '@@dpkg'; cat /var/lib/dpkg/status; }
[ -r /lib/apk/db/installed ] && { echo '@@apk'; cat /lib/apk/db/installed; }
command -v find >/dev/null 2>&1 || exit 0
find / -xdev \( -path /proc -o -path /sys -o -path /tmp -o -path /var/cache \) -prune -o \
  \( -path '*-packages/*.dist-info/METADATA' -o -path '*-packages/*.egg-info/PKG-INFO' \) -print 2>/dev/null | head -3000 |
  while IFS= read -r m; do echo '@@py'; head -c 4000 "$m" | grep -m2 -E '^(Name|Version):'; done
find / -xdev \( -path /proc -o -path /sys -o -path /tmp \) -prune -o -path '*node_modules/.package-lock.json' -print 2>/dev/null |
  head -20 | while IFS= read -r l; do echo '@@npm'; cat "$l"; done
"""


async def _images() -> list[dict]:
    code, out = await _run("docker", "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}")
    if code != 0:
        return []
    by_image: dict[str, dict] = {}
    for line in out.splitlines():
        cid, name, ref = (line.split("\t") + ["", "", ""])[:3]
        code, image_id = await _run("docker", "inspect", "-f", "{{.Image}}", cid)
        entry = by_image.setdefault(image_id.strip() or ref, {"refs": set(), "containers": [], "cid": cid})
        entry["containers"].append(name)
        if not re.fullmatch(r"(sha256:)?[0-9a-f]{12,64}", ref):
            entry["refs"].add(re.sub(r":[0-9a-f]{12,64}$", "", ref))    # хеш коммита в теге меняется каждую выкатку
    images = []
    for image_id, entry in by_image.items():
        label = sorted(entry["refs"])[0] if entry["refs"] else entry["containers"][0]
        item = {"label": label, "containers": sorted(entry["containers"]), "os": "", "packages": [], "env": {}}
        code, env_out = await _run("docker", "image", "inspect", "-f", "{{json .Config.Env}}", image_id)
        try:
            env = dict(e.split("=", 1) for e in json.loads(env_out or "[]") or [] if "=" in e)
            item["env"] = {k: env[k] for k in RUNTIME_ENV if k in env}
        except ValueError:
            pass
        code, probe = await _run("docker", "exec", entry["cid"], "sh", "-c", PROBE, timeout=EXEC_SEC)
        if code == 127 and not probe:
            item["error"] = "no_shell"      # distroless: без sh внутри смотреть нечем
        _parse_probe(probe, item)
        images.append(item)
    return images


def _sections(text: str):
    name, buf = None, []
    for line in text.splitlines():
        if line.startswith("@@") and line[2:] in ("os", "dpkg", "apk", "py", "npm"):
            if name:
                yield name, "\n".join(buf)
            name, buf = line[2:], []
        else:
            buf.append(line)
    if name:
        yield name, "\n".join(buf)


def _parse_probe(text: str, item: dict) -> None:
    eco_os, seen, pkgs = "", set(), item["packages"]

    def add(eco: str, name: str, version: str) -> None:
        key = (eco, name, version)
        if name and version and key not in seen and len(pkgs) < MAX_PACKAGES:
            seen.add(key)
            pkgs.append({"eco": eco, "name": name, "version": version})

    for kind, body in _sections(text):
        if kind == "os":
            rel = _os_release(body)
            item["os"] = rel.get("PRETTY_NAME", "")
            os_id, ver = rel.get("ID", ""), rel.get("VERSION_ID", "")
            item["os_id"], item["os_version"] = os_id, ver
            if os_id == "debian":
                eco_os = "Debian:" + ver.split(".")[0]
            elif os_id == "ubuntu":
                eco_os = f"Ubuntu:{ver}" + (":LTS" if ver.endswith(".04") and int(ver.split(".")[0]) % 2 == 0 else "")
            elif os_id == "alpine":
                eco_os = "Alpine:v" + ".".join(ver.split(".")[:2])
        elif kind == "dpkg" and eco_os:
            for block in body.split("\n\n"):
                fields = dict(re.findall(r"^([A-Za-z-]+): ?(.*)$", block, re.M))
                if "install ok installed" not in fields.get("Status", ""):
                    continue
                # OSV для Debian/Ubuntu знает пакеты по имени исходника: «Source: openssl (3.0.13-1)»
                src = fields.get("Source", fields.get("Package", ""))
                m = re.match(r"(\S+)(?: \((\S+)\))?", src)
                add(eco_os, m.group(1) if m else src, (m.group(2) if m and m.group(2) else fields.get("Version", "")))
        elif kind == "apk" and eco_os:
            for block in body.split("\n\n"):
                fields = dict(re.findall(r"^([A-Za-z]):(.*)$", block, re.M))
                add(eco_os, fields.get("o") or fields.get("P", ""), fields.get("V", ""))
        elif kind == "py":
            name = re.search(r"^Name: *(.+)$", body, re.M)
            ver = re.search(r"^Version: *(.+)$", body, re.M)
            if name and ver:
                add("PyPI", name.group(1).strip(), ver.group(1).strip())
        elif kind == "npm":
            try:
                lock = json.loads(body)
            except ValueError:
                continue
            for path, meta in (lock.get("packages") or {}).items():
                if path and isinstance(meta, dict) and meta.get("version"):
                    add("npm", meta.get("name") or path.rsplit("node_modules/", 1)[-1], meta["version"])


# ── сокеты ───────────────────────────────────────────────────────────────────

def _public(host: str) -> bool:
    host = host.strip("[]").split("%")[0]
    if host in ("*", "0.0.0.0", "::", ""):
        return True
    try:
        return not any(ipaddress.ip_address(host) in net for net in PRIVATE)
    except ValueError:
        return False


async def _listeners() -> list[dict]:
    found, seen = [], set()
    if shutil.which("ss"):
        code, out = await _run("ss", "-H", "-tulnp")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            host, _, port = parts[4].rpartition(":")
            proc = re.search(r'users:\(\("([^"]+)"', line)
            if _public(host):
                key = (parts[0], port, proc.group(1) if proc else "")
                if key not in seen:
                    seen.add(key)
                    found.append({"proto": parts[0], "port": int(port) if port.isdigit() else port,
                                  "process": key[2], "addr": host})
    elif platform.system() == "Windows":
        for x in _json_list(await _ps(PS_LISTEN, timeout=60)):
            host, port = str(x.get("a") or ""), x.get("p")
            if _public(host):
                key = ("tcp", str(port), str(x.get("n") or ""))
                if key not in seen:
                    seen.add(key)
                    found.append({"proto": "tcp", "port": port, "process": key[2], "addr": host})
    elif shutil.which("lsof"):
        code, out = await _run("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-iUDP")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 9:
                continue
            host, _, port = parts[8].rpartition(":")
            proto = "udp" if parts[7].upper() == "UDP" else "tcp"
            if _public(host):
                key = (proto, port, parts[0])
                if key not in seen:
                    seen.add(key)
                    found.append({"proto": proto, "port": int(port) if port.isdigit() else port,
                                  "process": parts[0], "addr": host})
    return found


async def _runtimes() -> dict:
    out = {}
    for name, cmd in (("node", ("node", "--version")), ("python", ("python3", "--version")),
                      ("docker", ("docker", "version", "--format", "{{.Server.Version}}"))):
        code, text = await _run(*cmd, timeout=20)
        if code == 0 and text.strip():
            out[name] = text.strip().lstrip("v").replace("Python ", "")
    return out
