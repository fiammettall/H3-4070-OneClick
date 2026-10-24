param([ValidateSet('install','prepare','start','stop','check','models','pause','repair')][string]$Action = 'install')
$ErrorActionPreference = 'Stop'
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONNOUSERSITE = '1'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:DO_NOT_TRACK = '1'
$h3Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$h3State = Join-Path $h3Root '_state'
$h3Cache = Join-Path $h3Root '.cache'
New-Item -ItemType Directory -Path $h3State,$h3Cache,(Join-Path $h3Root 'logs') -Force | Out-Null
$h3Python = Join-Path $h3Root 'runtime\python.exe'
$h3Config = Get-Content -LiteralPath (Join-Path $h3Root 'locks\runtime.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$h3Lock = $null
$h3RuntimeLock = $null

function Get-PrivatePython {
    param([switch]$Repair)
    if (Test-Path -LiteralPath $h3Python) {
        $h3Version = & $h3Python -I -c 'import platform,ssl,ensurepip,json,zipfile; print(platform.python_version())'
        if ($LASTEXITCODE -eq 0 -and $h3Version -eq $h3Config.python_version -and -not $Repair) { return }
        if (-not $Repair) { throw '独立 Python 缺失组件或版本不匹配，请使用修复入口恢复；不会删除用户输入输出。' }
    }
    if (-not [Environment]::Is64BitOperatingSystem) { throw '本安装包需要 64 位 Windows。' }
    $h3Archive = $h3Config.archives | Where-Object { $_.destination -eq 'runtime' }
    $h3Zip = Join-Path $h3Cache $h3Archive.file
    if (Test-Path -LiteralPath $h3Zip) {
        if ((Get-FileHash -LiteralPath $h3Zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $h3Archive.sha256) {
            Move-Item -LiteralPath $h3Zip -Destination ($h3Zip + '.bad-' + [Guid]::NewGuid().ToString('N'))
            Write-Host '已保留损坏的 Python 缓存，正在重新下载。'
        }
    }
    if (-not (Test-Path -LiteralPath $h3Zip)) {
        Write-Host '正在准备独立 Python。不会修改系统 Python 或系统 PATH。'
        $h3Partial = $h3Zip + '.partial'
        if (Test-Path -LiteralPath $h3Partial) {
            $h3PartialSize = (Get-Item -LiteralPath $h3Partial).Length
            if ($h3PartialSize -eq $h3Archive.size -and (Get-FileHash -LiteralPath $h3Partial -Algorithm SHA256).Hash.ToLowerInvariant() -eq $h3Archive.sha256) {
                Move-Item -LiteralPath $h3Partial -Destination $h3Zip
            } elseif ($h3PartialSize -ge $h3Archive.size) {
                Move-Item -LiteralPath $h3Partial -Destination ($h3Partial + '.bad-' + [Guid]::NewGuid().ToString('N'))
            }
        }
        if (-not (Test-Path -LiteralPath $h3Zip)) {
        & curl.exe --location --fail --show-error --continue-at - --connect-timeout 20 --retry 3 --retry-all-errors --speed-limit 1024 --speed-time 60 --output $h3Partial $h3Archive.url
        if ($LASTEXITCODE -ne 0) { throw 'Python 下载中断，可再次双击安装继续。' }
        if ((Get-Item -LiteralPath $h3Partial).Length -ne $h3Archive.size -or (Get-FileHash -LiteralPath $h3Partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $h3Archive.sha256) {
            Move-Item -LiteralPath $h3Partial -Destination ($h3Partial + '.bad-' + [Guid]::NewGuid().ToString('N'))
            throw 'Python 下载校验失败，已保留损坏文件；再次运行安装将重新下载。'
        }
        # 源和目标均是安装目录内的固定缓存文件。
        Move-Item -LiteralPath $h3Partial -Destination $h3Zip
        }
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $h3ZipHandle = [System.IO.Compression.ZipFile]::OpenRead($h3Zip)
    try {
        $h3Runtime = [System.IO.Path]::GetFullPath((Join-Path $h3Root 'runtime'))
        foreach ($h3Entry in $h3ZipHandle.Entries) {
            if (Test-Path -LiteralPath (Join-Path $h3State 'cancel_download.flag')) { throw '已暂停，缓存已保留，再次运行修复可继续。' }
            if (-not $h3Entry.FullName.StartsWith('tools/', [StringComparison]::Ordinal)) { continue }
            $h3Relative = $h3Entry.FullName.Substring(6).Replace('/', '\')
            if (-not $h3Relative) { continue }
            $h3Target = [System.IO.Path]::GetFullPath((Join-Path $h3Runtime $h3Relative))
            if (-not $h3Target.StartsWith($h3Runtime + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '压缩包含不安全路径。' }
            if ($h3Entry.FullName.EndsWith('/')) {
                New-Item -ItemType Directory -Path $h3Target -Force | Out-Null
            } else {
                New-Item -ItemType Directory -Path (Split-Path -Parent $h3Target) -Force | Out-Null
                [System.IO.Compression.ZipFileExtensions]::ExtractToFile($h3Entry, $h3Target, $true)
            }
        }
    } finally { $h3ZipHandle.Dispose() }
    & $h3Python -I -c 'import platform,ssl,pip; print(platform.python_version(),pip.__version__)'
    if ($LASTEXITCODE -ne 0) { throw '独立 Python 启动失败，请查看日志或检查 Windows 运行库。' }
}

try {
    if ($Action -eq 'pause') {
        [System.IO.File]::WriteAllBytes((Join-Path $h3State 'cancel_download.flag'), [byte[]](49))
        Write-Host '已请求暂停本包的安装和下载；文件断点会保留。'
        exit 0
    }
    if ($Action -in @('install','prepare','models','repair')) {
        try { $h3Lock = [System.IO.File]::Open((Join-Path $h3State 'bootstrap.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
        catch { throw '已有安装或下载正在运行，请不要重复打开。' }
        $h3Cancel = Join-Path $h3State 'cancel_download.flag'
        if (Test-Path -LiteralPath $h3Cancel) { Remove-Item -LiteralPath $h3Cancel }
        # 启动器使用同一安装锁；引导阶段也不能覆盖运行中的解释器。
        try { $h3RuntimeLock = [System.IO.File]::Open((Join-Path $h3State 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
        catch { throw '安装器正在运行，请等待完成后再试。' }
        $h3Services = Join-Path $h3State 'services.json'
        if (Test-Path -LiteralPath $h3Services) {
            $h3Records = Get-Content -LiteralPath $h3Services -Raw -Encoding UTF8 | ConvertFrom-Json
            if (@($h3Records.PSObject.Properties).Count -gt 0) { throw '本包仍有服务身份记录，请先使用停止服务入口，再安装或修复。' }
        }
        Get-PrivatePython -Repair:($Action -eq 'repair')
        $h3RuntimeLock.Dispose()
        $h3RuntimeLock = $null
        if (Test-Path -LiteralPath $h3Cancel) { throw '已暂停，独立 Python 文件已保留；再次运行安装可继续。' }
        if ($Action -eq 'prepare') { & $h3Python -I (Join-Path $PSScriptRoot 'install.py') --prepare-only }
        elseif ($Action -eq 'models') { & $h3Python -I (Join-Path $PSScriptRoot 'install.py') --models-only }
        elseif ($Action -eq 'repair') { & $h3Python -I (Join-Path $PSScriptRoot 'install.py') --repair }
        else { & $h3Python -I (Join-Path $PSScriptRoot 'install.py') }
    } else {
        if (-not (Test-Path -LiteralPath $h3Python)) { throw '请先双击 1_安装或继续下载.cmd。' }
        & $h3Python -I (Join-Path $PSScriptRoot 'manage.py') $Action
    }
    if ($Action -eq 'check' -and $LASTEXITCODE -eq 2) {
        Write-Host '安装尚未完成，已显示当前阶段。请完成安装后再启动生成。'
        exit 0
    }
    if ($LASTEXITCODE -ne 0) { throw "操作未完成，退出码 $LASTEXITCODE；请查看 logs 和 _state 内的记录。" }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($null -ne $h3RuntimeLock) { $h3RuntimeLock.Dispose() }
    if ($null -ne $h3Lock) { $h3Lock.Dispose() }
}
