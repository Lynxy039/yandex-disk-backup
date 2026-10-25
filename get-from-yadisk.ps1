$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$appArgs = @($args)
$python = $null
$pythonArgs = @()

$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($launcher) {
    & $launcher.Source -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 1>$null 2>$null
    if ($LASTEXITCODE -eq 0) {
        $python = $launcher.Source
        $pythonArgs = @('-3')
    }
}
if (-not $python) {
    $candidate = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.Source -notlike '*\WindowsApps\*') {
        & $candidate.Source -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 1>$null 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $candidate.Source }
    }
}
if (-not $python) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        Write-Host 'Python и winget не найдены. Установите Python с https://www.python.org/downloads/windows/ и запустите этот файл снова.'
        exit 1
    }
    Write-Host 'Python не найден. Устанавливаю Python 3.13 для текущего пользователя через winget...'
    & winget.exe install --exact --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    if (-not (Test-Path $python)) {
        Write-Host 'Python установлен. Закройте это окно и запустите лаунчер снова, чтобы обновился PATH.'
        exit 1
    }
}

& $python @pythonArgs (Join-Path $PSScriptRoot 'bootstrap.py') @appArgs
exit $LASTEXITCODE
