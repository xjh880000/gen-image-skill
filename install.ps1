# ============================================================
#  gen-image skill · 一键安装（Windows）
#
#  用法（在本文件所在目录打开 PowerShell）：
#    powershell -ExecutionPolicy Bypass -File .\install.ps1 "sk-你的key"
#
#  或者从网上一行搞定：
#    irm <安装脚本地址> | iex
# ============================================================
param(
    [string]$Key = ""
)

$ErrorActionPreference = "Stop"
$RepoOwner = "__REPO_OWNER__"
$RepoName  = "gen-image-skill"
$Branch    = "main"
$SkillName = "gen-image"

function Ok($m)   { Write-Host "  " -NoNewline; Write-Host "OK " -ForegroundColor Green -NoNewline; Write-Host $m }
function Warn($m) { Write-Host "  " -NoNewline; Write-Host "!  " -ForegroundColor Yellow -NoNewline; Write-Host $m }
function Err($m)  { Write-Host "  " -NoNewline; Write-Host "X  " -ForegroundColor Red -NoNewline; Write-Host $m }
function Dim($m)  { Write-Host $m -ForegroundColor DarkGray }

Write-Host ""
Write-Host "  gen-image skill 安装程序"
Write-Host "  -----------------------------"
Write-Host ""

# ---------------------------------------------------------------- 1. Python
$Py = $null
foreach ($c in @("python", "python3", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            $v = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            $parts = $v -split '\.'
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 8) { $Py = $c; break }
        } catch { }
    }
}
if (-not $Py) {
    Err "没找到 Python 3.8+"
    Write-Host ""
    Write-Host "  先装 Python，再回来跑这个脚本："
    Write-Host "    https://www.python.org/downloads/"
    Write-Host "    安装时记得勾上 'Add Python to PATH'"
    exit 1
}
$pyVer = & $Py -V 2>&1
Ok "Python：$pyVer"

# ---------------------------------------------------------------- 2. 找安装目录
$targets = @()
if ($env:GEN_IMAGE_INSTALL_DIR) {
    # 隔离安装（测试/自定义位置用）
    New-Item -ItemType Directory -Force -Path $env:GEN_IMAGE_INSTALL_DIR | Out-Null
    $targets = @($env:GEN_IMAGE_INSTALL_DIR)
    Ok "指定安装目录：$env:GEN_IMAGE_INSTALL_DIR"
} else {
    foreach ($b in @("$env:USERPROFILE\.dsh\skills", "$env:USERPROFILE\.claude\skills", "$env:USERPROFILE\.codex\skills")) {
        if (Test-Path $b) { $targets += $b }
    }
}
if ($targets.Count -eq 0) {
    $def = "$env:USERPROFILE\.claude\skills"
    New-Item -ItemType Directory -Force -Path $def | Out-Null
    $targets = @($def)
    Warn "没检测到现成的 AI 客户端 skills 目录，已创建 $def"
}

# ---------------------------------------------------------------- 3. 取源码
$src = $null
$selfDir = $PSScriptRoot
if ($selfDir -and (Test-Path (Join-Path $selfDir "scripts\gen_image.py"))) {
    $src = $selfDir
    Ok "用本地文件安装（$src）"
} else {
    $tmp = Join-Path $env:TEMP ("gen-image-" + [guid]::NewGuid().ToString("N").Substring(0,8))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $url = "https://github.com/$RepoOwner/$RepoName/archive/refs/heads/$Branch.zip"
    Write-Host "  正在下载…"
    try {
        $zip = Join-Path $tmp "pkg.zip"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 120
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $found = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like "$RepoName-*" } | Select-Object -First 1
        if (-not $found) { throw "解压后找不到源码目录" }
        $src = $found.FullName
        Ok "下载完成"
    } catch {
        Err "下载失败：$($_.Exception.Message)"
        Write-Host "  地址：$url"
        exit 1
    }
}

# ---------------------------------------------------------------- 4. 复制
$installed = @()
foreach ($base in $targets) {
    $dest = Join-Path $base $SkillName
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item -Path (Join-Path $src "*") -Destination $dest -Recurse -Force
    # 清掉不该带过去的东西
    if (Test-Path (Join-Path $dest ".git")) { Remove-Item (Join-Path $dest ".git") -Recurse -Force -ErrorAction SilentlyContinue }
    Get-ChildItem -Path $dest -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $dest -Recurse -File -Filter "_patch*.py" -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    foreach ($f in @("install.ps1", "install.sh")) {
        $p = Join-Path $dest $f
        if (Test-Path $p) { Remove-Item $p -Force -ErrorAction SilentlyContinue }
    }
    $installed += $dest
    Ok "已装到 $dest"
}

# ---------------------------------------------------------------- 5. 写配置
if ([string]::IsNullOrWhiteSpace($Key)) {
    Write-Host ""
    Warn "没给 key。装是装好了，但还不能出图。"
    Write-Host "  补上 key 的办法：编辑下面任意一个 config.json，把 key 填进去"
    foreach ($d in $installed) { Write-Host "    $(Join-Path $d 'config.json')" }
} else {
    $cfgObj = [ordered]@{
        base  = "https://xyuapi.top/v1"
        key   = $Key.Trim()
        model = "gpt-image-2.5-flare"
        chain = @("gpt-image-2.5-flare", "NanoBanana-Pro", "gpt-image-2-pro", "gpt-image-2.5-sunburst")
    }
    $json = $cfgObj | ConvertTo-Json -Depth 5
    foreach ($d in $installed) {
        $cfgPath = Join-Path $d "config.json"
        [System.IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    }
    Ok "已写入 key"
}

# ---------------------------------------------------------------- 6. 装依赖
Write-Host ""
$hasReq = $false
try { & $Py -c "import requests" 2>$null; if ($LASTEXITCODE -eq 0) { $hasReq = $true } } catch { }
if ($hasReq) {
    Ok "requests 已存在"
} else {
    Write-Host "  正在装依赖 requests…"
    & $Py -m pip install --quiet --user requests 2>$null
    if ($LASTEXITCODE -eq 0) { Ok "requests 装好了" }
    else { Warn "自动装依赖失败。手动跑一次：$Py -m pip install requests" }
}

# ---------------------------------------------------------------- 7. 自检
Write-Host ""
Write-Host "  连通性自检…"
$first = $installed[0]
$genScript = Join-Path $first "scripts\gen_image.py"
$probeOk = $false
try {
    & $Py $genScript --list-models 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $probeOk = $true }
} catch { }
if ($probeOk) {
    Ok "接口通了，key 有效"
} else {
    Warn "自检没通过。手动跑这条看详细报错："
    Write-Host "    $Py `"$genScript`" --list-models"
}

Write-Host ""
Write-Host "  -----------------------------"
Ok "装完了"
Write-Host ""
Write-Host "  怎么用："
Write-Host "    1) 用 Claude Code / DSH 这类 AI 客户端时，直接说："
Write-Host "       「帮我生成一张图：一只橘猫坐在窗台，午后暖光」"
Write-Host ""
Write-Host "    2) 不用 AI、直接命令行："
Write-Host "       $Py `"$genScript`" -p `"一只橘猫坐在窗台，午后暖光`""
Write-Host ""
Write-Host "  图会存在当前目录的 gen-image-out\ 里。"
Write-Host ""
