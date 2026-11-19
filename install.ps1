# Мост Yune на Windows одной командой (сайт отдаёт этот файл по /connect/windows с подставленным адресом):
#   irm https://yune.jp/connect/windows | iex
# или прямо из открытого репозитория (последний релиз):
#   irm https://raw.githubusercontent.com/ronskii2/yune-bridge/main/install.ps1 | iex
# Ставит мост в %USERPROFILE%\.yune-bridge-app, привязывает машину кодом и включает автозапуск при входе (Планировщик).
# Архив — релиз GitHub (тег = сборка) или с сайта; хеш содержимого сверяется со сборкой до установки.
$ErrorActionPreference = "Stop"
$Site = if ($env:YUNE_SITE) { $env:YUNE_SITE } else { "__YUNE_SITE__" }
if ($Site -like "__*__") { $Site = "https://yune.jp" }
$Source = if ($env:YUNE_BRIDGE_SOURCE) { $env:YUNE_BRIDGE_SOURCE } else { "__BRIDGE_SOURCE__" }
if ($Source -like "__*__") { $Source = "github" }
$Build = "__BRIDGE_BUILD__"
if ($Build -like "__*__") { $Build = "" }
# язык сообщений: подставляет сайт (язык витрины); из репозитория — язык Windows; мосту — тот же (код привязки)
$Ui = if ($env:YUNE_LANG) { $env:YUNE_LANG } else { "__YUNE_LANG__" }
if ($Ui -like "__*__" -or -not $Ui) { $Ui = (Get-Culture).TwoLetterISOLanguageName }
if (@("ja", "en", "ru") -notcontains $Ui) { $Ui = "en" }
$env:YUNE_LANG = $Ui
$Msg = @{
  ja = @{ python = "Python 3.11 以上が必要です（python.org または Microsoft Store）"; download = "ブリッジをダウンロード中: {0}";
          verify = "ブリッジのアーカイブがビルドの検証に通りませんでした"; done = "完了: ブリッジを起動しました。Windows にサインインすると自動で起動します。" }
  en = @{ python = "Python 3.11 or newer is required (python.org or Microsoft Store)"; download = "Downloading the bridge: {0}";
          verify = "The bridge archive failed the build check"; done = "Done: the bridge is running and starts when you sign in to Windows." }
  ru = @{ python = "Нужен Python 3.11 или новее (python.org или Microsoft Store)"; download = "Скачиваю мост: {0}";
          verify = "Архив моста не прошёл проверку сборки"; done = "Готово: мост запущен и стартует сам при входе в Windows." }
}[$Ui]
$Repo = "https://github.com/ronskii2/yune-bridge"
$App = Join-Path $HOME ".yune-bridge-app"
$Py = (Get-Command py -ErrorAction SilentlyContinue)
if ($Py) { $PyCmd = "py"; $PyArgs = @("-3") } else { $PyCmd = "python"; $PyArgs = @() }
& $PyCmd @PyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { Write-Error $Msg.python; exit 1 }

if ($Source -eq "github") {
  $Zip = if ($Build) { "$Repo/releases/download/$Build/yune-bridge.zip" } else { "$Repo/releases/latest/download/yune-bridge.zip" }
} else {
  $Zip = "$Site/connect/bridge.zip"
}

New-Item -ItemType Directory -Force -Path $App | Out-Null
Set-Location $App
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("yune-bridge-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
Write-Host ($Msg.download -f $Zip)
Invoke-WebRequest $Zip -OutFile (Join-Path $Tmp "bridge.zip") -UseBasicParsing
# хеш содержимого считает сам установщик (тот же алгоритм, что update.build_of) — не код из архива; не сошлось — стоп
$Verify = @'
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
'@
Set-Content -Path (Join-Path $Tmp "verify.py") -Value $Verify -Encoding ASCII
$VerifyArgs = @((Join-Path $Tmp "verify.py"), (Join-Path $Tmp "bridge.zip"), (Join-Path $Tmp "x"))
if ($Build) { $VerifyArgs += $Build }
& $PyCmd @PyArgs @VerifyArgs
if ($LASTEXITCODE -ne 0) { Remove-Item -Recurse -Force $Tmp; Write-Error $Msg.verify; exit 1 }
Remove-Item -Recurse -Force claude_bridge, requirements.txt -ErrorAction SilentlyContinue   # прежняя сборка целиком
Move-Item (Join-Path $Tmp "x\claude_bridge") .
Move-Item (Join-Path $Tmp "x\requirements.txt") .
Remove-Item -Recurse -Force $Tmp
if ($Source -eq "github") {          # дальше мост обновляется только релизами GitHub (update.pinned)
  $Data = Join-Path $HOME ".yune-bridge"
  New-Item -ItemType Directory -Force -Path $Data | Out-Null
  Set-Content -Path (Join-Path $Data "update-source") -Value "github" -Encoding ASCII
}
if (-not (Test-Path .venv)) { & $PyCmd @PyArgs -m venv .venv }
# Пакеты — с PyPI без настроек pip человека: чужой индекс в pip.ini (pypi.ngc.nvidia.com и т. п.), который не открывается,
# вешал установку на повторах. Не вышло — ещё раз по его настройкам (своё зеркало, прокси)
function Install-Pip {
  & .venv\Scripts\python -m pip install -q --isolated --index-url https://pypi.org/simple --retries 2 --timeout 20 @args
  if ($LASTEXITCODE -ne 0) { & .venv\Scripts\python -m pip install -q @args }
}
Install-Pip --upgrade pip | Out-Null
Install-Pip -r requirements.txt

if (-not (Test-Path (Join-Path $HOME ".yune-bridge\config.json"))) {
  & .venv\Scripts\python -m claude_bridge.remote pair $Site --no-wait   # ввода кода ждёт мост: команда не висит
}

$Action = New-ScheduledTaskAction -Execute (Join-Path $App ".venv\Scripts\pythonw.exe") -Argument "-m claude_bridge.remote run" -WorkingDirectory $App
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "Yune bridge" -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Stop-ScheduledTask -TaskName "Yune bridge" -ErrorAction SilentlyContinue   # повторная установка — с новым мостом и кодом
Start-ScheduledTask -TaskName "Yune bridge"
Write-Host $Msg.done
