# ============================================================
#  凝思 Studio 冒烟测试（无需浏览器）
#  对"正在运行的服务"发真实 HTTP 请求，跑完一次快速会话并核对产物。
#
#  本文件保存为 UTF-8 BOM（PowerShell 5.1 默认按 GBK 读脚本，无 BOM 时中文会破坏解析）。
#  用法：  .\scripts\smoke.ps1 [-BaseUri http://127.0.0.1:8765] [-Participant p99] [-Speed 0.05]
# ============================================================
[CmdletBinding()]
param(
    [string]$BaseUri = "http://127.0.0.1:8765",
    [string]$Participant = "p99",
    [double]$Speed = 0.05,
    [int]$TimeoutSec = 600
)

$ErrorActionPreference = "Continue"
$Root     = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$failures = New-Object System.Collections.ArrayList

function Write-Step([string]$Text) { Write-Host ("  -> " + $Text) -ForegroundColor Cyan }
function Write-Warn([string]$Text) { Write-Host ("  [!] " + $Text) -ForegroundColor Yellow }
function Write-Err([string]$Text)  { Write-Host ("  [x] " + $Text) -ForegroundColor Red }
function Add-Failure([string]$Text) { [void]$failures.Add($Text); Write-Err $Text }
function Check([string]$Label, [bool]$Ok, [string]$Detail = "") {
    if ($Ok) { Write-Host ("  [通过] " + $Label + " " + $Detail) -ForegroundColor Green }
    else { Add-Failure ($Label + " " + $Detail) }
}

Write-Host ""
Write-Host "=== 凝思 Studio 冒烟测试 ===" -ForegroundColor Green
Write-Host ("  目标地址：" + $BaseUri)

# 1) 基础接口 ---------------------------------------------------------------
try {
    $health = Invoke-RestMethod -Uri "$BaseUri/api/health" -Method Get -TimeoutSec 10
} catch {
    Write-Err ("连不上服务：" + $_.Exception.Message)
    Write-Warn "请先启动服务：双击 run.bat 选 1，或执行 .\run.ps1 -Action serve"
    exit 2
}
Check "服务健康状态" ($health.status -eq "ok") ("产品版本=" + $health.product)
Check "频谱口径 welch-v1" ($health.engine.spectrum -eq "welch-v1") ("实际=" + $health.engine.spectrum)
$dataDir = $health.data_dir

$config = Invoke-RestMethod -Uri "$BaseUri/api/config" -Method Get
Check "分析窗与步长" (($config.window_sec -eq 4.0) -and ($config.step_sec -eq 2.0)) ("窗=" + $config.window_sec + "s 步长=" + $config.step_sec + "s")
$devices = Invoke-RestMethod -Uri "$BaseUri/api/devices?probe=0.3" -Method Get
Check "数据源列表" ($devices.sources.Count -ge 1) ("共 " + $devices.sources.Count + " 个")
$openapi = Invoke-RestMethod -Uri "$BaseUri/api/openapi.json" -Method Get
Check "接口描述可用" ($openapi.info.title.Length -gt 0) ("端点数=" + ($openapi.paths.PSObject.Properties.Name.Count))

# 2) 被试 -------------------------------------------------------------------
# 与后端编号规则一致：去空格、去 sub- 前缀、转小写
$publicId = $Participant.Trim().ToLowerInvariant()
if ($publicId.StartsWith("sub-")) { $publicId = $publicId.Substring(4) }
if ($publicId -notmatch '^[a-z]{0,3}\d{1,4}$') {
    Add-Failure ("被试编号不合法：'" + $Participant + "'（应形如 p01 / sub-p01）")
    Write-Host ""
    Write-Host ("冒烟测试失败：" + $failures.Count + " 项") -ForegroundColor Red
    exit 1
}
try {
    $subject = Invoke-RestMethod -Uri "$BaseUri/api/subjects" -Method Post -TimeoutSec 15 `
        -ContentType "application/json; charset=utf-8" `
        -Body (@{ public_id = $publicId; label = "冒烟测试"; note = "smoke.ps1" } | ConvertTo-Json -Compress)
    Write-Step ("已就绪被试 sub-" + $subject.public_id)
} catch {
    Write-Warn "被试创建失败（可能已存在），继续后续步骤"
}
try {
    $subjectDetail = Invoke-RestMethod -Uri "$BaseUri/api/subjects/$publicId" -Method Get -TimeoutSec 10
    Check "读取被试详情" ($subjectDetail.public_id -eq $publicId) ("编号=" + $subjectDetail.public_id)
} catch {
    Add-Failure ("读取被试详情失败：" + $_.Exception.Message)
}

# 3) 创建会话 ---------------------------------------------------------------
$body = @{
    participant   = $publicId
    device        = "sim-bsense"
    time_scale    = $Speed
    training_mode = "quick"
    label         = "冒烟测试"
} | ConvertTo-Json -Compress
$created = Invoke-RestMethod -Uri "$BaseUri/api/sessions" -Method Post -TimeoutSec 20 `
    -ContentType "application/json; charset=utf-8" -Body $body
$uuid = $created.session.uuid
Check "创建会话" ([bool]$uuid) ("会话=" + $uuid)

# 4) 等待跑完 ---------------------------------------------------------------
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$status = $created.session.status
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $detail = Invoke-RestMethod -Uri "$BaseUri/api/sessions/$uuid" -Method Get
    $status = $detail.status
    Write-Host ("     阶段：" + $detail.phase + "  进度：" + [math]::Round($detail.progress * 100) + "%")
    if ($status -in @("done", "failed", "cancelled")) { break }
}
Write-Host ""
$detail = Invoke-RestMethod -Uri "$BaseUri/api/sessions/$uuid" -Method Get
Check "会话正常结束" ($detail.status -eq "done") ("状态=" + $detail.status + " 阶段=" + $detail.phase)
if ($detail.status -ne "done") {
    foreach ($run in $detail.runs) {
        if ($run.error) { Write-Err ("阶段 " + $run.phase + " 失败：" + $run.error) }
    }
}

# 5) 结果与产物 -------------------------------------------------------------
if ($detail.status -eq "done") {
    $report = Invoke-RestMethod -Uri "$BaseUri/api/sessions/$uuid/report" -Method Get
    Check "报告口径" ($report.spec -eq "joint-assessment-v1") ("实际=" + $report.spec)
    Check "报告内口径版本" ($report.versions.spectrum -eq "welch-v1") ("频谱=" + $report.versions.spectrum)
    Check "联合评估结论" ([bool]$report.assessment.conclusion) ("结论=" + $report.assessment.conclusion)
    Check "结论边界声明" ($report.assessment.boundary.Length -gt 0) ("边界=" + $report.assessment.boundary)
    Check "量表结果（SAS+SDS）" ($report.scales.PSObject.Properties.Name.Count -ge 2) ("量表=" + ($report.scales.PSObject.Properties.Name -join "、"))
    Check "行为任务结果" (($report.behavior.sart.trials -eq 180) -and ($report.behavior.pvt.valid -eq $true)) ("SART 试次=" + $report.behavior.sart.trials)

    $heat = Invoke-RestMethod -Uri "$BaseUri/api/sessions/$uuid/heatmap" -Method Get
    Check "热力图单元格" ($heat.cells.Count -gt 0) ("共 " + $heat.cells.Count + " 格")
    # 缺失窗的 score 是 $null，不能直接参与数值比较（否则断言恒真、假通过）
    $scored = @($heat.cells | Where-Object { $_.index -ge 0 -and $null -ne $_.score })
    $allInRange = $scored.Count -gt 0
    foreach ($cell in $scored) { if ($cell.score -lt 0 -or $cell.score -gt 1) { $allInRange = $false } }
    Check "已评分格在 0~1" $allInRange ("已评分=" + $scored.Count + " 缺失=" + ($heat.cells.Count - $scored.Count) + " 接口自报已评分=" + $heat.scored)
    Check "缺失格计数一致" (($heat.cells.Count - $scored.Count) -eq $heat.missing) ("缺失=" + $heat.missing)

    $trend = Invoke-RestMethod -Uri "$BaseUri/api/reports/trend?field=focus&period=week" -Method Get
    Check "趋势数据点" ($trend.points.Count -ge 1) ("点数=" + $trend.points.Count)

    $arts = Invoke-RestMethod -Uri "$BaseUri/api/sessions/$uuid/artifacts" -Method Get
    $kinds = @($arts.items | ForEach-Object { $_.kind })
    foreach ($kind in @("report_md", "report_json", "heatmap_svg", "trend_svg")) {
        Check ("产物 " + $kind) ($kinds -contains $kind) ""
    }
    $missing = @($arts.items | Where-Object { -not $_.exists })
    Check "产物文件都在磁盘上" ($missing.Count -eq 0) ("缺失=" + $missing.Count)

    # 二进制附件：Windows PowerShell 5.1 的 Invoke-WebRequest 会抛 NullReference，
    # 因此这里用 .NET WebClient 下载
    $zipBytes = -1
    $zipError = ""
    try {
        $client = New-Object System.Net.WebClient
        try {
            $zipBytes = $client.DownloadData("$BaseUri/api/sessions/$uuid/export.zip").Length
        } finally {
            $client.Dispose()
        }
    } catch {
        $zipError = $_.Exception.Message
    }
    Check "打包下载 export.zip" ($zipBytes -gt 500) ("字节=" + $zipBytes + " " + $zipError)
    Write-Host ("  数据目录：" + $dataDir)
}

# 6) 错误语义 ---------------------------------------------------------------
try {
    Invoke-RestMethod -Uri "$BaseUri/api/sessions/ffffffffffffffffffffffffffffffff" -Method Get -TimeoutSec 10 | Out-Null
    Add-Failure "不存在的会话应返回 404"
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    Check "不存在的会话返回 404" ($code -eq 404) ("实际=" + $code)
}

Write-Host ""
if ($failures.Count -eq 0) {
    Write-Host "冒烟测试通过" -ForegroundColor Green
    exit 0
}
Write-Host ("冒烟测试失败：" + $failures.Count + " 项") -ForegroundColor Red
foreach ($item in $failures) { Write-Host ("  - " + $item) -ForegroundColor Red }
exit 1
