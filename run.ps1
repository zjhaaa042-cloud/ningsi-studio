# ============================================================
#  凝思 Studio 一键启动器
#
#  注意：本文件必须以 **UTF-8 BOM** 保存。
#  Windows PowerShell 5.1 默认按 ANSI/GBK 读取脚本文件，
#  没有 BOM 时中文会破坏语法解析（报 "Missing closing '}'"）。
#
#  用法：  .\run.ps1 [-Action <名称>] [-Port 8765] [-Participant p01] [-Speed 0.05]
#  动作：  menu serve demo test check ui-check smoke doctor bootstrap
# ============================================================
[CmdletBinding()]
param(
    [ValidateSet("menu", "serve", "demo", "test", "check", "ui-check", "smoke", "doctor", "bootstrap")]
    [string]$Action = "menu",
    [int]$Port = 8765,
    [string]$Participant = "p01",
    [double]$Speed = 0.05,
    [switch]$Open,
    [switch]$Recreate
)

$ErrorActionPreference = "Continue"
try { [void][Console]::OutputEncoding; [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

# 任何未预期错误：打印出来并停住窗口，避免双击运行时窗口一闪而过
trap {
    Write-Host ""
    Write-Host ("  [x] 发生未预期错误：" + $_.Exception.Message) -ForegroundColor Red
    Write-Host ("      位置：" + $_.InvocationInfo.PositionMessage)
    Write-Host ""
    Write-Host "  请把上面的内容复制给我，便于定位问题。"
    Read-Host "  按回车键退出" | Out-Null
    exit 3
}

$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPy  = Join-Path $Root ".venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = Join-Path $Root "src"
$script:Ok = 0

function Write-Step([string]$Text) { Write-Host ("  -> " + $Text) -ForegroundColor Cyan }
function Write-Warn([string]$Text) { Write-Host ("  [!] " + $Text) -ForegroundColor Yellow }
function Write-Err([string]$Text)  { Write-Host ("  [x] " + $Text) -ForegroundColor Red }

function Resolve-Python {
    if (Test-Path $VenvPy) { return $VenvPy }
    foreach ($candidate in @("python", "python3", "py")) {
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

function Get-Diagnosis {
    # 依赖体检表：一次把该看的都打出来，省得反复试
    $lines = New-Object System.Collections.ArrayList
    if ($script:Python) {
        [void]$lines.Add("  python          : " + $script:Python)
    } else {
        [void]$lines.Add("  python          : 未找到")
    }
    if ($script:Python) {
        $version = (& $script:Python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>&1 | Out-String).Trim()
        $note = ""
        if ($version -match "^(\d+)\.(\d+)") {
            $major = [int]::Parse($Matches[1])
            $minor = [int]::Parse($Matches[2])
            if (($major -lt 3) -or (($major -eq 3) -and ($minor -lt 11))) { $note = "   <-- 需要 Python 3.11 或更高" }
        }
        [void]$lines.Add("  python 版本     : " + $version + $note)
        $modules = @(
            @{ name = "numpy";         desc = "必需（信号处理）" },
            @{ name = "ningsi";        desc = "必需（算法引擎）" },
            @{ name = "ningsi_studio"; desc = "必需（本项目）" },
            @{ name = "pylsl";         desc = "可选（接真实脑电设备）" }
        )
        foreach ($item in $modules) {
            & $script:Python -c ("import " + $item.name) 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                [void]$lines.Add(("  {0,-15}: 正常    （{1}）" -f $item.name, $item.desc))
            } else {
                [void]$lines.Add(("  {0,-15}: 缺失    （{1}）" -f $item.name, $item.desc))
            }
        }
    }
    return ($lines -join [Environment]::NewLine)
}

function Test-Engine {
    param([string]$Python)
    if (-not $Python) { return $false }
    $srcPath = Join-Path $Root "src"
    $env:PYTHONPATH = $srcPath
    & $Python -c "import ningsi_studio, ningsi" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { return $true }
    # 退一步：用同一工作区里并排放着的引擎源码
    foreach ($relative in @("..\ningsi\src", "..\ningsi\ningsi\src", "vendor\ningsi\src")) {
        $candidate = Join-Path $Root $relative
        if (Test-Path (Join-Path $candidate "ningsi\__init__.py")) {
            $env:PYTHONPATH = $srcPath + ";" + $candidate
            & $Python -c "import ningsi_studio, ningsi" 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Host ("     引擎取自源码目录：" + $candidate) -ForegroundColor DarkGray
                return $true
            }
        }
    }
    return $false
}

function Invoke-Py {
    param([string[]]$CmdArgs)
    if (-not $script:Python -or "$($script:Python)".Trim() -eq "") {
        Write-Err "没有可用的 Python 解释器。"
        $script:Ok = 2
        return
    }
    # 用参数数组直接调用解释器（不经命令行字符串拼装），Anaconda 等发行版下最稳
    Write-Host ("     " + $script:Python + " " + ($CmdArgs -join " ")) -ForegroundColor DarkGray
    Push-Location $Root
    try {
        & $script:Python @CmdArgs
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        $script:Ok = $code
    } finally {
        Pop-Location
    }
}

function Invoke-Bootstrap {
    $bootstrapPath = Join-Path $Root "scripts\bootstrap.ps1"
    if (-not (Test-Path $bootstrapPath)) {
        Write-Err ("找不到安装脚本：" + $bootstrapPath)
        $script:Ok = 2
        return
    }
    $cliArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $bootstrapPath)
    if ($Recreate) { $cliArgs += "-Recreate" }
    $hostExe = Get-Command pwsh -ErrorAction SilentlyContinue
    $exe = "powershell"
    if ($hostExe) { $exe = $hostExe.Source }
    Write-Step "正在准备运行环境（虚拟环境 + 依赖）"
    Push-Location $Root
    try {
        & $exe @cliArgs
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        $script:Ok = $code
    } finally {
        Pop-Location
    }
}

function Show-Menu {
    Write-Host ""
    Write-Host "  凝思 Studio · A09 专注力强化训练与心理状态评估" -ForegroundColor Green
    Write-Host ("  项目目录：" + $Root)
    Write-Host ""
    Write-Host "  [1] serve      启动网站服务（界面 + 接口），并打印访问地址"
    Write-Host "  [2] demo       不开浏览器，直接跑完一次完整会话（快速模式）"
    Write-Host "  [3] test       运行自动化测试（含前端 DOM 回归，约 2 分钟）"
    Write-Host "  [4] check      静态自检：语法、接口/事件文档、前端资源一致性"
    Write-Host "  [5] ui-check   真实浏览器自检：点按钮 + 看实时波形（需 Edge）"
    Write-Host "  [6] smoke      对正在运行的服务做冒烟测试（需先用 1 启动）"
    Write-Host "  [7] doctor     环境自检：依赖是否齐全、数据库统计"
    Write-Host "  [8] bootstrap  准备运行环境（建虚拟环境并安装依赖）"
    Write-Host "  [0] quit       退出"
    Write-Host ""
    $pick = (Read-Host "  请输入编号（0-8，0=退出）").Trim()
    switch ($pick) {
        "1" { return "serve" }
        "2" { return "demo" }
        "3" { return "test" }
        "4" { return "check" }
        "5" { return "ui-check" }
        "6" { return "smoke" }
        "7" { return "doctor" }
        "8" { return "bootstrap" }
        "0" { return "quit" }
        "" { return "doctor" }
        default { return "menu" }
    }
}

# ---------------------------------------------------------------- 主流程

Write-Host ""
Write-Host "=== 凝思 Studio 启动器 ===" -ForegroundColor Green
Write-Host ("  项目目录：" + $Root)

$interactive = ($Action -eq "menu")

$script:Python = Resolve-Python
if ($script:Python) { Write-Host ("  Python：" + $script:Python) }
else { Write-Host "  Python：未找到" -ForegroundColor Yellow }

$engineReady = Test-Engine -Python $script:Python
if (-not $engineReady) {
    Write-Warn "还没准备好：ningsi / ningsi_studio 目前无法导入。"
    Write-Host (Get-Diagnosis)
    Write-Host ""
    $bootstrapPath = Join-Path $Root "scripts\bootstrap.ps1"
    if (Test-Path $bootstrapPath) {
        Write-Host "  是否现在自动准备环境（建 .venv 并安装依赖）？直接回车即开始。" -ForegroundColor Yellow
        $answer = (Read-Host "  现在安装依赖吗？(Y/n)").Trim().ToLower()
        if ($answer -ne "n" -and $answer -ne "no") {
            Invoke-Bootstrap
            $script:Python = Resolve-Python
            $engineReady = Test-Engine -Python $script:Python
        }
    }
    if (-not $engineReady) {
        Write-Err "依赖检查未通过，已停止（没有启动任何服务）。"
        Write-Host ""
        Write-Host "  也可以手动处理（任选一种）："
        Write-Host "    1) 在本目录执行：  .\scripts\bootstrap.ps1"
        Write-Host "    2) 执行：          pip install -e ..\ningsi"
        Write-Host "    3) 临时指定路径：  set PYTHONPATH=..\ningsi\src;..\ningsi"
        Write-Host ""
        Read-Host "  按回车键退出" | Out-Null
        exit 2
    }
    if (-not $interactive) {
        Write-Step "依赖已就绪，继续执行你指定的动作。"
    }
}

while ($true) {
    if ($interactive) {
        $Action = Show-Menu
    }
    if ($Action -eq "quit") {
        Write-Host "  已退出。"
        if ($interactive) { Read-Host "  按回车键关闭窗口" | Out-Null }
        exit 0
    }

    switch ($Action) {
        "doctor" {
            Invoke-Py -CmdArgs @("-m", "ningsi_studio", "doctor")
        }
        "bootstrap" {
            Invoke-Bootstrap
            $script:Python = Resolve-Python
        }
        "demo" {
            Invoke-Py -CmdArgs @("-m", "ningsi_studio", "demo", "--participant", $Participant,
                                 "--speed", "$Speed")
        }
        "test" {
            Invoke-Py -CmdArgs @("-m", "unittest", "discover", "-s", "tests", "-t", ".")
            if ($script:Ok -eq 0) { Write-Host "     全部用例通过" -ForegroundColor Green }
            else { Write-Err "有用例未通过（见上面的输出）" }
        }
        "check" {
            Invoke-Py -CmdArgs @("-m", "ningsi_studio", "check")
            if ($script:Ok -eq 0) { Write-Host "     静态自检通过" -ForegroundColor Green }
            else { Write-Err "静态自检发现问题（见上面的列表）" }
        }
        "ui-check" {
            Write-Step "真实浏览器自检（自动起服务与仿真 LSL 流，点按钮并验证实时波形）"
            Invoke-Py -CmdArgs @("-m", "ningsi_studio", "ui-check")
            if ($script:Ok -eq 0) { Write-Host "     界面自检通过：按钮可点、实时脑电已绘制" -ForegroundColor Green }
            else { Write-Err "界面自检未通过（见上面的 [失败] 行）" }
        }
        "smoke" {
            $smokePath = Join-Path $Root "scripts\smoke.ps1"
            $hostExe = Get-Command pwsh -ErrorAction SilentlyContinue
            $exe = "powershell"
            if ($hostExe) { $exe = $hostExe.Source }
            Write-Step ("对 http://127.0.0.1:" + $Port + " 做冒烟测试")
            & $exe -NoProfile -ExecutionPolicy Bypass -File $smokePath -BaseUri ("http://127.0.0.1:" + $Port) -Participant $Participant
            $script:Ok = $LASTEXITCODE
        }
        "serve" {
            $url = "http://127.0.0.1:" + $Port + "/"
            $health = "http://127.0.0.1:" + $Port + "/api/health"
            Write-Host ""

            # 端口上已经有本服务在跑：直接复用，避免起第二个实例漂到别的端口
            $already = $false
            try {
                $probe = Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 2
                if ($probe.StatusCode -eq 200) { $already = $true }
            } catch { }

            if ($already) {
                Write-Host ("  服务已经在运行：" + $url) -ForegroundColor Green
                if ($Open) { try { Start-Process $url } catch { } }
                Write-Host "  （如果只是想看看页面，按 [0] 退出即可）"
                $script:Ok = 0
            } else {
                # 端口被别的程序占用时明确提示，不要让服务悄悄漂到 8766
                $portBusy = $false
                try {
                    $client = New-Object System.Net.Sockets.TcpClient
                    $client.Connect("127.0.0.1", $Port)
                    $client.Close()
                    $portBusy = $true
                } catch { }

                if ($portBusy) {
                    Write-Err ("端口 " + $Port + " 已被其他程序占用（不是本服务）。")
                    Write-Host "  换一个端口再试，例如："
                    Write-Host ("    .\run.ps1 -Action serve -Port 8790")
                    Write-Host ("    或者在命令行执行： python -m ningsi_studio serve --port 8790")
                    Write-Host ""
                    $script:Ok = 1
                } else {
                    Write-Step ("正在启动服务，端口 " + $Port + " ...")
                    if (-not $script:Python -or "$($script:Python)".Trim() -eq "") {
                        Write-Err "没有可用的 Python 解释器。"
                        $script:Ok = 2
                    } else {
                        # 用后台作业启动：随时可停，且不依赖 Start-Process 的参数数组
                        $pythonPath = [string]$script:Python
                        $projectRoot = [string]$Root
                        $job = Start-Job -ArgumentList @($pythonPath, $projectRoot, $Port) -ScriptBlock {
                            param($py, $dir, $port)
                            $env:PYTHONIOENCODING = "utf-8"
                            $env:PYTHONPATH = $dir + "\src"
                            Set-Location -LiteralPath $dir
                            & $py -m ningsi_studio serve --port $port
                        }

                        # 等服务真正开始监听（最多 40 秒）
                        $ready = $false
                        $deadline = (Get-Date).AddSeconds(40)
                        while ((Get-Date) -lt $deadline) {
                            if ($job.State -eq "Failed" -or $job.State -eq "Completed") { break }
                            Start-Sleep -Milliseconds 500
                            try {
                                $probe = Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 2
                                if ($probe.StatusCode -eq 200) { $ready = $true; break }
                            } catch { }
                        }

                        if ($ready) {
                            Write-Host ("  服务已就绪：" + $url) -ForegroundColor Green
                            Write-Host ("  接口地址    ：" + $health)
                            Write-Host "  想停止服务  ：在下面按回车" -ForegroundColor Yellow
                            Write-Host ""
                            if ($Open) {
                                try { Start-Process $url } catch { Write-Warn ("打不开浏览器：" + $_.Exception.Message) }
                            } else {
                                Write-Host ("  请在浏览器打开：" + $url)
                            }
                            Read-Host "  按回车停止服务并回到菜单" | Out-Null
                            Stop-Job -Job $job -ErrorAction SilentlyContinue
                            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
                            $script:Ok = 0
                        } else {
                            Write-Err ("服务在端口 " + $Port + " 上没能就绪。")
                            Write-Host ("  后台作业状态：" + $job.State)
                            $output = Receive-Job -Job $job 2>&1
                            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
                            if ($output) {
                                Write-Host "  ---- Python 输出 ----"
                                $output | ForEach-Object { Write-Host ("  " + $_) }
                                Write-Host "  ---- 输出结束 ----"
                            } else {
                                Write-Host "  Python 没有输出（可能是端口被占用）。"
                            }
                            Write-Host ""
                            Write-Host "  建议依次检查："
                            Write-Host "    [6] doctor     环境自检"
                            Write-Host "    [4] check      静态自检"
                            Write-Host "    命令行：       python -m ningsi_studio serve --port 8766"
                            Write-Host ""
                            $script:Ok = 1
                        }
                    }
                }
            }
        }
        "menu" {
            $interactive = $true
            continue
        }
        default { Write-Warn ("未知动作：" + $Action) }
    }

    Write-Host ""
    if ($script:Ok -eq 0) { Write-Host "  执行结果：成功" -ForegroundColor Green }
    else { Write-Host ("  执行结果：失败（退出码 " + $script:Ok + "）") -ForegroundColor Red }

    if (-not $interactive) { break }
    Write-Host ""
    $again = (Read-Host "  回到菜单继续吗？(Y/n)").Trim().ToLower()
    if ($again -eq "n" -or $again -eq "no") { break }
}

if ($interactive) { Read-Host "  按回车键关闭窗口" | Out-Null }
exit $script:Ok
