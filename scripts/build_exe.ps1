# Package ningsi-studio into a Windows exe with PyInstaller.
#
# ASCII only on purpose: Windows PowerShell 5.1 reads .ps1 as ANSI/GBK, non-ASCII bytes break parsing.
#
#   .\scripts\build_exe.ps1                  # single file: dist\ningsi-studio.exe
#   .\scripts\build_exe.ps1 -Mode onedir     # folder build: dist\ningsi-studio\ (starts faster)
#   .\scripts\build_exe.ps1 -Clean           # remove build/ and dist/ first
#   .\scripts\build_exe.ps1 -NoPip           # do not try to pip install pyinstaller
#
# What gets bundled:
#   - the studio package (src/ningsi_studio) and the front end (web/) as data
#   - the engine package (ningsi) from the sibling checkout
#   - numpy and pylsl (pylsl is collected with --collect-all so liblsl*.dll comes along)
#
# Runtime behaviour of the exe is decided in ningsi_studio/settings.py:
#   web/  comes from the bundle (sys._MEIPASS), writable data goes next to the exe
#   (var\studio) and falls back to %LOCALAPPDATA%\ningsi-studio\var\studio if that is read-only.

param(
    [ValidateSet("onefile", "onedir")]
    [string]$Mode = "onefile",
    [string]$Name = "ningsi-studio",
    [switch]$Clean,
    [switch]$NoPip
)

# Continue, not Stop: PyInstaller writes progress to stderr, and with -ErrorActionPreference Stop
# PowerShell 5.1 would treat that as a terminating error mid-build. Failures are checked explicitly
# through $LASTEXITCODE below.
$ErrorActionPreference = "Continue"
$Studio = Split-Path -Parent $PSScriptRoot          # ...\ningsi-studio
$Root = Split-Path -Parent $Studio                  # workspace root
$Dist = Join-Path $Studio "dist"
$Work = Join-Path $Studio "build\pyinstaller"
$Spec = Join-Path $Studio "build"
$Entry = Join-Path $PSScriptRoot "frozen_entry.py"

function Write-Step([string]$Text) { Write-Host ("  [build] " + $Text) }
function Fail([string]$Text) { Write-Host ("  [build] ERROR: " + $Text) -ForegroundColor Red; exit 1 }

# ------------------------------------------------------------------ python
$Python = Join-Path $Studio ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) { Fail "python not found; run .\run.ps1 -Action bootstrap first" }
    $Python = $found.Source
}
Write-Step ("python      : " + $Python)

# ------------------------------------------------------------------ sources
$StudioSrc = Join-Path $Studio "src"
if (-not (Test-Path (Join-Path $StudioSrc "ningsi_studio\__init__.py"))) {
    Fail ("studio package not found under " + $StudioSrc)
}
$EngineCandidates = @(
    (Join-Path $Root "ningsi\src"),
    (Join-Path $Root "ningsi\ningsi\src"),
    (Join-Path $Studio "vendor\ningsi\src")
)
$EngineSrc = $null
foreach ($candidate in $EngineCandidates) {
    if (Test-Path (Join-Path $candidate "ningsi\__init__.py")) { $EngineSrc = $candidate; break }
}
if (-not $EngineSrc) { Fail "engine package ningsi not found (looked in ningsi\src, ningsi\ningsi\src, vendor\ningsi\src)" }
Write-Step ("engine src  : " + $EngineSrc)

$WebDir = Join-Path $Studio "web"
if (-not (Test-Path (Join-Path $WebDir "index.html"))) { Fail ("front end not found: " + $WebDir) }
Write-Step ("front end   : " + $WebDir)

# ------------------------------------------------------------------ build time deps
if (-not $NoPip) {
    & $Python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Step "installing pyinstaller ..."
        & $Python -m pip install --disable-pip-version-check pyinstaller
        if ($LASTEXITCODE -ne 0) { Fail "pip install pyinstaller failed" }
    }
}

if ($Clean) {
    foreach ($target in @($Dist, (Join-Path $Studio "build"))) {
        if (Test-Path $target) { Remove-Item -Recurse -Force $target; Write-Step ("removed " + $target) }
    }
}
New-Item -ItemType Directory -Force -Path $Dist, $Work, $Spec | Out-Null

# ------------------------------------------------------------------ pyinstaller
# PYTHONPATH first so the packages are taken from src/ (a stale editable .pth pointing at a
# moved checkout would otherwise win, which is exactly the failure mode this repo hit before).
$env:PYTHONPATH = ($StudioSrc + ";" + $EngineSrc)

$ModeFlag = if ($Mode -eq "onefile") { "--onefile" } else { "--onedir" }
$Arguments = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--log-level", "WARN",
    $ModeFlag,
    "--name", $Name,
    "--distpath", $Dist,
    "--workpath", $Work,
    "--specpath", $Spec,
    "--paths", $StudioSrc,
    "--paths", $EngineSrc,
    "--add-data", ($WebDir + ";web"),
    "--collect-all", "pylsl",
    "--collect-submodules", "ningsi",
    "--collect-submodules", "ningsi_studio",
    # schema.sql 是包内的运行期数据文件（建库时读）。--collect-submodules 只收模块，
    # --collect-data 在本机实测没有把它带进来，所以这里再显式 add-data 一份（幂等）。
    "--collect-data", "ningsi_studio",
    "--add-data", ((Join-Path $StudioSrc "ningsi_studio\db\schema.sql") + ";ningsi_studio/db"),
    "--exclude-module", "tkinter",
    "--exclude-module", "matplotlib",
    "--exclude-module", "pandas",
    "--exclude-module", "scipy",
    "--exclude-module", "PyQt5",
    "--exclude-module", "IPython",
    $Entry
)
Write-Step ("mode        : " + $Mode)
Write-Step ("running PyInstaller ...")
$Log = Join-Path $Spec "pyinstaller.log"
& $Python @Arguments *> $Log
$Code = $LASTEXITCODE
if ($Code -ne 0) {
    Get-Content $Log -Tail 40 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ("    " + $_) }
    Fail ("PyInstaller failed with exit code " + $Code + " (full log: " + $Log + ")")
}
Write-Step ("log         : " + $Log)

# ------------------------------------------------------------------ result
$Exe = Join-Path $Dist ($Name + ".exe")
if ($Mode -eq "onefile") {
    if (-not (Test-Path $Exe)) { Fail ("expected exe not produced: " + $Exe) }
    $size = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
    Write-Host ""
    Write-Host ("  [build] OK  " + $Exe + "  (" + $size + " MB)")
    Write-Host ("  [build] try:  `"$Exe`" doctor")
    Write-Host ("  [build] run:  `"$Exe`"            # starts the web service and opens the browser")
} else {
    if (-not (Test-Path $Exe)) { Fail ("expected exe not produced: " + $Exe) }
    Write-Host ""
    Write-Host ("  [build] OK  " + (Join-Path $Dist $Name) + "\  (folder build)")
    Write-Host ("  [build] try:  `"$Exe`" doctor")
}
