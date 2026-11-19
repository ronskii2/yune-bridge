#!/bin/sh
# Мост Yune на Mac или Linux одной командой (сайт отдаёт этот файл по /connect/install с подставленным адресом):
#   curl -fsSL https://yune.jp/connect/install | sh
# или прямо из открытого репозитория (последний релиз):
#   curl -fsSL https://raw.githubusercontent.com/ronskii2/yune-bridge/main/install.sh | sh
# Ставит мост в ~/.yune-bridge-app (свой Python venv), привязывает машину кодом (ввести в «Подключениях») и включает
# автозапуск: launchd на Mac, systemd --user на Linux. Повторный запуск обновляет мост и не просит код заново.
# Архив — релиз GitHub (тег = сборка) или с сайта; хеш содержимого сверяется со сборкой (python -m claude_bridge.update).
set -e
SITE="${YUNE_SITE:-__YUNE_SITE__}"
case "$SITE" in __*__) SITE="https://yune.jp" ;; esac
SOURCE="${YUNE_BRIDGE_SOURCE:-__BRIDGE_SOURCE__}"
case "$SOURCE" in __*__) SOURCE="github" ;; esac
BUILD="__BRIDGE_BUILD__"
case "$BUILD" in __*__) BUILD="" ;; esac
UI="${YUNE_LANG:-__YUNE_LANG__}"        # язык сообщений: подставляет сайт (язык витрины); из репозитория — локаль машины
case "$UI" in __*__|"") case "${LC_ALL:-${LANG:-}}" in ja*) UI=ja ;; ru*) UI=ru ;; *) UI=en ;; esac ;; esac
case "$UI" in ja|en|ru) ;; *) UI=en ;; esac
export YUNE_LANG="$UI"                  # и мосту: код привязки — на том же языке
msg() {
  case "$UI:$1" in
    ja:python) echo "Python 3.11 以上が必要です。Mac: brew install python、Linux: python3.11 以上のパッケージをインストールしてください" ;;
    en:python) echo "Python 3.11 or newer is required. Mac: brew install python, Linux: a python3.11+ package" ;;
    ru:python) echo "Нужен Python 3.11 или новее. Mac: brew install python, Linux: пакет python3.11+" ;;
    ja:noagent) echo "注意: Claude Code も Codex も見つかりません。どちらかをインストールすれば、ブリッジが自動で認識します。" ;;
    en:noagent) echo "Note: neither Claude Code nor Codex was found — install at least one, the bridge will pick it up." ;;
    ru:noagent) echo "Внимание: ни Claude Code, ни Codex не найдены — поставьте хотя бы один, мост увидит его сам." ;;
    ja:download) echo "ブリッジをダウンロード中: $2" ;;
    en:download) echo "Downloading the bridge: $2" ;;
    ru:download) echo "Скачиваю мост: $2" ;;
    ja:done_mac) echo "完了: ブリッジを起動しました。ログイン時に自動で起動します。ログ: ~/.yune-bridge/bridge.log" ;;
    en:done_mac) echo "Done: the bridge is running and starts at login. Log: ~/.yune-bridge/bridge.log" ;;
    ru:done_mac) echo "Готово: мост запущен и стартует сам при входе. Журнал: ~/.yune-bridge/bridge.log" ;;
    ja:done_linux) echo "完了: ブリッジを起動しました（systemctl --user status yune-bridge）。" ;;
    en:done_linux) echo "Done: the bridge is running (systemctl --user status yune-bridge)." ;;
    ru:done_linux) echo "Готово: мост запущен (systemctl --user status yune-bridge)." ;;
    ja:done_other) echo "完了。ブリッジを起動してください: $2" ;;
    en:done_other) echo "Done. Start the bridge: $2" ;;
    ru:done_other) echo "Готово. Запустите мост: $2" ;;
  esac
}
REPO="https://github.com/ronskii2/yune-bridge"
APP="$HOME/.yune-bridge-app"
PY="${PYTHON:-python3}"

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  msg python >&2
  exit 1
fi
command -v claude >/dev/null 2>&1 || command -v codex >/dev/null 2>&1 || \
  msg noagent

if [ "$SOURCE" = "github" ]; then
  if [ -n "$BUILD" ]; then ZIP="$REPO/releases/download/$BUILD/yune-bridge.zip"
  else ZIP="$REPO/releases/latest/download/yune-bridge.zip"; fi
else
  ZIP="$SITE/connect/bridge.zip"
fi

mkdir -p "$APP"
cd "$APP"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
msg download "$ZIP"
curl -fsSL "$ZIP" -o "$TMP/bridge.zip"
# хеш содержимого считает сам установщик (тот же алгоритм, что update.build_of) — не код из архива; не сошлось — стоп
"$PY" - "$TMP/bridge.zip" "$TMP/x" "$BUILD" <<'PY'
import hashlib, sys, zipfile
zpath, dest = sys.argv[1], sys.argv[2]
want = sys.argv[3] if len(sys.argv) > 3 else ''
z = zipfile.ZipFile(zpath)
h, declared = hashlib.sha256(), ''
for n in sorted(n for n in z.namelist() if not n.endswith('/')):
    if n == 'claude_bridge/build.txt':
        declared = z.read(n).decode().strip()
        continue
    if n != 'requirements.txt' and not (n.startswith('claude_bridge/') and n.count('/') == 1 and '..' not in n):
        sys.exit('unexpected file in archive: ' + n)
    h.update(n.encode() + b'\0' + hashlib.sha256(z.read(n)).digest())
build = h.hexdigest()[:12]
if build != declared or (want and build != want):
    sys.exit('build mismatch: archive ' + build + ', build.txt ' + (declared or '-') + ', expected ' + (want or '-'))
z.extractall(dest)
print('build ' + build + ' ok')
PY
rm -rf claude_bridge requirements.txt      # прежняя сборка целиком: старые модули не подхватятся
mv "$TMP/x/claude_bridge" "$TMP/x/requirements.txt" .
if [ "$SOURCE" = "github" ]; then          # дальше мост обновляется только релизами GitHub (update.pinned)
  mkdir -p "$HOME/.yune-bridge" && echo github > "$HOME/.yune-bridge/update-source"
fi
[ -d .venv ] || "$PY" -m venv .venv
# Пакеты — с PyPI без настроек pip человека: чужой индекс в pip.conf (pypi.ngc.nvidia.com и т. п.), который не открывается,
# вешал установку на повторах. Не вышло — ещё раз по его настройкам (своё зеркало, прокси)
pipi() { .venv/bin/pip install -q --isolated --index-url https://pypi.org/simple --retries 2 --timeout 20 "$@" || .venv/bin/pip install -q "$@"; }
pipi --upgrade pip >/dev/null
pipi -r requirements.txt

if [ ! -f "$HOME/.yune-bridge/config.json" ]; then
  .venv/bin/python -m claude_bridge.remote pair "$SITE" --no-wait   # ввода кода ждёт мост: команда не висит
fi

RUN="$APP/.venv/bin/python -m claude_bridge.remote run"
case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/jp.yune.bridge.plist"
    mkdir -p "$(dirname "$PLIST")"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>jp.yune.bridge</string>
  <key>ProgramArguments</key><array><string>$APP/.venv/bin/python</string><string>-m</string><string>claude_bridge.remote</string><string>run</string></array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATH</string><key>YUNE_LANG</key><string>$UI</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.yune-bridge/bridge.log</string>
  <key>StandardErrorPath</key><string>$HOME/.yune-bridge/bridge.log</string>
</dict></plist>
PLIST
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    msg done_mac
    ;;
  Linux)
    UNIT="$HOME/.config/systemd/user/yune-bridge.service"
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<UNIT
[Unit]
Description=Yune bridge (claude_bridge.remote)
After=network-online.target

[Service]
WorkingDirectory=$APP
ExecStart=$RUN
Environment=PATH=$PATH
Environment=YUNE_LANG=$UI
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable yune-bridge
    systemctl --user restart yune-bridge   # уже работал (повторная установка) — с новым мостом и новым кодом
    msg done_linux
    ;;
  *)
    msg done_other "$RUN"
    ;;
esac
