param([switch]$Stop, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$bridgeRoot = Split-Path $PSScriptRoot -Parent
$bridgeState = Join-Path $bridgeRoot '.runtime'
New-Item -ItemType Directory -Path $bridgeState -Force | Out-Null

function Invoke-BridgeCommand([string]$Python, [string]$Operation) {
    $bridgeInfo = [Diagnostics.ProcessStartInfo]::new()
    $bridgeInfo.FileName = $Python
    $bridgeInfo.Arguments = '-X utf8 -m bridge ' + $Operation
    $bridgeInfo.WorkingDirectory = $bridgeRoot
    $bridgeInfo.UseShellExecute = $false
    $bridgeInfo.CreateNoWindow = $true
    $bridgeInfo.RedirectStandardOutput = $true
    $bridgeInfo.RedirectStandardError = $true
    $bridgeProcess = [Diagnostics.Process]::Start($bridgeInfo)
    try {
        $bridgeOut = $bridgeProcess.StandardOutput.ReadToEndAsync()
        $bridgeErr = $bridgeProcess.StandardError.ReadToEndAsync()
        if (-not $bridgeProcess.WaitForExit(10000)) { $bridgeProcess.Kill(); throw '本机服务检查超时。' }
        $null = $bridgeOut.Result
        $null = $bridgeErr.Result
        return $bridgeProcess.ExitCode
    } finally { $bridgeProcess.Dispose() }
}

function Test-BridgePython([string]$Command, [string[]]$Prefix = @()) {
    try {
        $bridgeFound = & $Command @Prefix -c 'import sys,ssl; ssl.create_default_context(); print(sys.executable) if sys.version_info >= (3,10) else sys.exit(1)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $bridgeFound -and (Test-Path -LiteralPath ([string]$bridgeFound))) { return [string]$bridgeFound }
    } catch {}
    return $null
}

function Get-BridgePython {
    $bridgePrivate = Join-Path $bridgeState 'python/python.exe'
    $bridgeInterpreter = Test-BridgePython $bridgePrivate
    if ($bridgeInterpreter) { return $bridgeInterpreter }
    foreach ($bridgeCandidate in @('python.exe', 'python3.exe', 'py.exe')) {
        $bridgeCommand = Get-Command $bridgeCandidate -ErrorAction SilentlyContinue
        if ($bridgeCommand) {
            $bridgePrefix = @()
            if ($bridgeCandidate -eq 'py.exe') { $bridgePrefix = @('-3') }
            $bridgeInterpreter = Test-BridgePython $bridgeCommand.Source $bridgePrefix
            if ($bridgeInterpreter) { return $bridgeInterpreter }
        }
    }
    if ($Stop) { throw '未找到 Python。请先使用 start.cmd 安装本机运行环境。' }
    $bridgeInstallLock = [IO.File]::Open((Join-Path $bridgeState 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    try {
        $bridgeInterpreter = Test-BridgePython $bridgePrivate
        if ($bridgeInterpreter) { return $bridgeInterpreter }
        $bridgeArchitecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
        $bridgeRuntime = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'python-runtime.json') -Raw | ConvertFrom-Json
        $bridgePackage = $bridgeRuntime.$bridgeArchitecture
        if (-not $bridgePackage) { throw '不支持此 Windows 架构。请自行安装 Python 3.10+。' }
        $bridgeUri = [Uri]$bridgePackage.url
        if ($bridgeUri.Scheme -ne 'https' -or $bridgeUri.Host -ne 'www.python.org' -or $bridgePackage.sha256 -notmatch '^[0-9a-f]{64}$') { throw '运行环境下载清单无效。' }
        $bridgeArchive = Join-Path $bridgeState 'python-download.zip'
        Write-Host '正在从 python.org 下载独立 Python，首次启动需要联网…'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $bridgePackage.url -OutFile $bridgeArchive -UseBasicParsing -TimeoutSec 180
        if ((Get-FileHash -LiteralPath $bridgeArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $bridgePackage.sha256) { throw '下载校验失败，请检查网络后重试。' }
        $bridgeDestination = [IO.Path]::GetFullPath((Join-Path $bridgeState 'python'))
        $bridgeAllowed = [IO.Path]::GetFullPath($bridgeState) + [IO.Path]::DirectorySeparatorChar
        if (-not $bridgeDestination.StartsWith($bridgeAllowed, [StringComparison]::OrdinalIgnoreCase)) { throw '安装路径无效。' }
        if (Test-Path -LiteralPath $bridgeDestination) { throw '本机运行环境目录已存在但不可用。请自行安装 Python 3.10+ 后重试。' }
        Expand-Archive -LiteralPath $bridgeArchive -DestinationPath $bridgeDestination
        # The embedded distribution's isolated path must include the application root.
        $bridgePathFile = Get-ChildItem -LiteralPath $bridgeDestination -Filter '*._pth' | Select-Object -First 1
        [IO.File]::AppendAllText($bridgePathFile.FullName, ([Environment]::NewLine + '..\..' + [Environment]::NewLine))
        $bridgeInterpreter = Test-BridgePython $bridgePrivate
        if (-not $bridgeInterpreter) { throw '独立 Python 无法运行，请自行安装 Python 3.10+。' }
        Remove-Item -LiteralPath $bridgeArchive
        return $bridgeInterpreter
    } finally { $bridgeInstallLock.Dispose() }
}

try {
    $bridgePython = Get-BridgePython
    Push-Location -LiteralPath $bridgeRoot
    try {
        if ($Stop) {
            if ((Invoke-BridgeCommand $bridgePython '--stop') -ne 0) { throw '服务未运行或停止失败。若有请求正在处理，请等回复结束后重试。' }
            Write-Host '服务已停止。'
        } else {
            $bridgeAlreadyRunning = $false
            if ((Invoke-BridgeCommand $bridgePython '--status') -eq 0) { $bridgeAlreadyRunning = $true }
            if (-not $bridgeAlreadyRunning) {
                Start-Process -FilePath $bridgePython -ArgumentList @('-X', 'utf8', '-m', 'bridge', '--no-browser') -WorkingDirectory $bridgeRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $bridgeState 'stdout.log') -RedirectStandardError (Join-Path $bridgeState 'stderr.log') | Out-Null
            }
            $bridgeReady = $false
            for ($bridgeAttempt = 0; $bridgeAttempt -lt 40; $bridgeAttempt++) {
                if ((Invoke-BridgeCommand $bridgePython '--status') -eq 0) { $bridgeReady = $true; break }
                Start-Sleep -Milliseconds 200
            }
            if (-not $bridgeReady) { throw '服务启动失败，请查看 .runtime/stderr.log。' }
            if (-not $NoBrowser) {
                if ((Invoke-BridgeCommand $bridgePython '--open-running') -ne 0) { throw '无法打开浏览器，请检查系统默认浏览器。' }
                Write-Host '设置页已打开。保存上游地址后，在页面点击“启动代理”。'
            } else { Write-Host '本机设置服务已就绪。' }
        }
    } finally { Pop-Location }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
