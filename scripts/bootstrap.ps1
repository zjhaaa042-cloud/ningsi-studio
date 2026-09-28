# ============================================================
#  凝思 Studio 环境准备：建虚拟环境并安装依赖
#
#  本文件必须保存为 UTF-8 BOM（PowerShell 5.1 默认按 GBK 读脚本，
#  没有 BOM 时中文会破坏解析）。加 BOM：python scripts\add_bom.py
#
#  用法：  .\scripts\bootstrap.ps1 [-Recreate] [-SkipEngineCheck] [-ForceVenv]
#
#  默认策略：优先使用"已经能导入依赖"的系统解释器（例如已经装好 numpy 与 pylsl 的
#  Anaconda），只有依赖不齐时才建 .venv —— 免得凭空多出一套环境反而缺东西。
#  想强制隔离环境就加 -ForceVenv。
# ============================================================
[CmdletBinding()]
param(
    [switch]$Recreate,
    [switch]$SkipEngineCheck,
    [switch]$ForceVenv
)

$ErrorActionPreference = "Continue"
$Root    = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VenvDir = Join-Path $Root ".venv"
$VenvPy  = Join-Path $VenvDir "Scripts\python.exe"

function Write-Step([string]$Text) { Write-Host ("  -> " + $Text) -ForegroundColor Cyan }
function Write-Warn([string]$Text) { Write-Host ("  [!] " + $Text) -ForegroundColor Yellow }
function Write-Err([string]$Text)  { Write-Host ("  [x] " + $Text) -ForegroundColor Red }

function Resolve-SystemPython {
    $candidates = @()
    if ($env:PYTHON) { $candidates += $env:PYTHON }
    $candidates += @("python", "python3", "py")
    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    $bases = @()
    if ($env:LOCALAPPDATA) { $bases += (Join-Path $env:LOCALAPPDATA "Programs\Python") }
    $bases += @("C:\Python313", "C:\Python312", "C:\Python311", "C:\Python310")
    foreach ($base in $bases) {
        if (-not (Test-Path $base)) { continue }
        $hit = Get-ChildItem -Path $base -Filter "python.exe" -Recurse -Depth 2 -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

Write-Host ""
Write-Host "=== 凝思 Studio 环境准备 ===" -ForegroundColor Green
Write-Host ("  项目目录：" + $Root)

# 1) 选解释器 ----------------------------------------------------------------
# 优先用"依赖已经齐"的系统解释器；只有不齐（或显式 -ForceVenv）才建 .venv。
function Test-Ready([string]$Py) {
    if (-not $Py) { return $false }
    & $Py -c "import numpy, pylsl, ningsi_studio" 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

$Python = $null
$system = Resolve-SystemPython
if ($Recreate -and (Test-Path $VenvDir)) {
    Write-Step "按 -Recreate 要求删除已有 .venv"
    Remove-Item -Recurse -Force $VenvDir
}

if ((-not $ForceVenv) -and (Test-Ready $system)) {
    $Python = $system
    Write-Host ("  使用系统解释器：" + $Python) -ForegroundColor Green
    Write-Host "  （依赖已齐，无需新建虚拟环境；想强制隔离请加 -ForceVenv）"
} else {
    if (-not (Test-Path $VenvPy)) {
        if (-not $system) {
            Write-Err "没有找到 Python。请先安装 Python 3.11 或更高版本，再运行本脚本。"
            exit 2
        }
        Write-Step ("创建虚拟环境 .venv（使用 " + $system + "）")
        & $system -m venv $VenvDir
    }
    if (-not (Test-Path $VenvPy)) {
        Write-Err "虚拟环境创建失败。"
        exit 2
    }
    $Python = $VenvPy
}
$version = (& $Python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>&1 | Out-String).Trim()
Write-Host ("  Python   ：" + $Python)
Write-Host ("  版本     ：" + $version)

# 2) 依赖 --------------------------------------------------------------------
Write-Step "升级 pip"
& $Python -m pip install --quiet --upgrade pip

$engineInstalled = $false
& $Python -c "import ningsi" 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  ningsi  ：已可用（无需安装）" -ForegroundColor Green
    $engineInstalled = $true
} else {
    foreach ($enginePath in @("..\ningsi", "..\ningsi\ningsi", "vendor\ningsi")) {
        $full = Join-Path $Root $enginePath
        if (Test-Path (Join-Path $full "pyproject.toml")) {
            Write-Step ("安装算法引擎：pip install -e " + $full)
            & $Python -m pip install --quiet -e $full
            & $Python -c "import ningsi" 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { $engineInstalled = $true; break }
        }
    }
}
if (-not $engineInstalled) {
    # 引擎目前**没有发布到 PyPI**（pip install ningsi 会失败），所以回退到官方仓库。
    # 若你已把它放到别处，先 `pip install -e <路径>` 再运行本脚本即可跳过这一步。
    Write-Step "从 GitHub 安装算法引擎：pip install git+https://github.com/zjhaaa042-cloud/ningsi.git"
    & $Python -m pip install --quiet "git+https://github.com/zjhaaa042-cloud/ningsi.git"
    & $Python -c "import ningsi" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $engineInstalled = $true }
}
if (-not $engineInstalled) {
    Write-Warn "算法引擎 ningsi 仍不可导入。可以试试："
    Write-Warn "  pip install -e ..\ningsi    （与 studio 并排放在同一父目录时）"
    Write-Warn "  pip install git+https://github.com/zjhaaa042-cloud/ningsi.git"
}

Write-Step "安装本项目：pip install -e ."
& $Python -c "import ningsi_studio" 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  ningsi-studio：已可用（无需安装）" -ForegroundColor Green
} else {
    & $Python -m pip install --quiet -e $Root
    & $Python -c "import ningsi_studio" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Err "本项目 ningsi-studio 不可导入，请查看上面的 pip 输出。" }
}

# 3) 报告 --------------------------------------------------------------------
Write-Host ""
Write-Step "依赖体检"
& $Python -c @"
mods = [
    ('numpy', '必需（信号处理）'),
    ('ningsi', '必需（算法引擎）'),
    ('ningsi_studio', '必需（本项目）'),
    ('pylsl', '可选（接真实脑电设备）'),
]
for name, note in mods:
    try:
        mod = __import__(name)
        version = getattr(mod, '__version__', '已安装')
        print('  [正常] %-14s %-12s %s' % (name, version, note))
    except Exception as exc:
        print('  [缺失] %-14s %-12s %s' % (name, exc.__class__.__name__, note))
"@

Write-Host ""
Write-Step "环境自检（doctor）"
$env:PYTHONPATH = Join-Path $Root "src"
& $Python -m ningsi_studio doctor

Write-Host ""
Write-Host "完成。下一步：双击 run.bat（菜单里选 1 启动服务）" -ForegroundColor Green
Write-Host ""
exit 0
