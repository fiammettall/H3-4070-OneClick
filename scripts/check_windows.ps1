chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
$failures = [System.Collections.Generic.List[string]]::new()

# 同时使用 Windows PowerShell 5.1 解析器和严格 UTF-8 解码；不执行被检查的脚本。
foreach ($script in Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1' -File) {
    $null = $strictUtf8.GetString([System.IO.File]::ReadAllBytes($script.FullName))
    $tokens = $null
    $parseErrors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($script.FullName, [ref]$tokens, [ref]$parseErrors)
    foreach ($issue in $parseErrors) { $failures.Add("$($script.Name): $($issue.Message)") }
    if ($script.Name -eq 'bootstrap.ps1') {
        $bytes = [System.IO.File]::ReadAllBytes($script.FullName)
        if ($bytes.Length -lt 3 -or $bytes[0] -ne 0xEF -or $bytes[1] -ne 0xBB -or $bytes[2] -ne 0xBF) {
            $failures.Add('bootstrap.ps1 必须保留 UTF-8 BOM，兼容 Windows PowerShell 5.1 中文。')
        }
    }
}

$entries = @(Get-ChildItem -LiteralPath $projectRoot -Filter '*.cmd' -File)
if ($entries.Count -lt 6) { $failures.Add('CMD 用户入口缺失。') }
foreach ($entry in $entries) {
    $bytes = [System.IO.File]::ReadAllBytes($entry.FullName)
    $content = $strictUtf8.GetString($bytes)
    # 当前批处理内容均为 ASCII，中文只用于文件名，避开 CMD 首行 BOM 兼容问题。
    if (@($bytes | Where-Object { $_ -gt 127 }).Count -ne 0) { $failures.Add("$($entry.Name): 批处理内容必须保持 ASCII。") }
    if ($content -notmatch '(?im)^chcp 65001\s*>nul\s*$') { $failures.Add("$($entry.Name): 缺少 UTF-8 代码页。") }
    if (-not $content.Contains('%~dp0scripts\bootstrap.ps1')) { $failures.Add("$($entry.Name): 启动路径必须基于批处理所在目录。") }
    if ($content -match '(?i)[A-Z]:\\') { $failures.Add("$($entry.Name): 存在本机绝对路径。") }
}

if ($failures.Count) { throw ($failures -join [Environment]::NewLine) }
Write-Output "PASS: PowerShell syntax / UTF-8 BOM / $($entries.Count) portable CMD entries."
