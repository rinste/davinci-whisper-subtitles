<#
    Whisper Subtitles for DaVinci Resolve - Windows installer.

        powershell -ExecutionPolicy Bypass -File install.ps1
        powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall

    UNTESTED: this script has never been run on a Windows machine. The macOS
    installer (install.sh) is the one that has been used in anger. If something
    here is wrong, please open an issue saying which Resolve version and Windows
    build you are on.

    It never touches your Resolve projects: it creates a virtualenv in this folder
    and puts the plugin in Resolve's Scripts folder.
#>

param([switch]$Uninstall)

$ErrorActionPreference = "Stop"

$Repo   = $PSScriptRoot
$Marker = Join-Path $HOME ".whisper-subtitles-resolve"

function Say  ($m) { Write-Host $m -ForegroundColor White }
function Warn ($m) { Write-Host $m -ForegroundColor Yellow }
function Die  ($m) { Write-Host $m -ForegroundColor Red; exit 1 }

# Resolve's own docs give two spellings for the user Scripts folder; use whichever
# parent actually exists on this machine.
function Get-ScriptsFolder {
    $candidates = @(
        (Join-Path $env:APPDATA "Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"),
        (Join-Path $env:APPDATA "Roaming\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"),
        (Join-Path $env:PROGRAMDATA "Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility")
    )
    foreach ($c in $candidates) {
        $parent = Split-Path (Split-Path $c -Parent) -Parent   # ...\Support\Fusion
        if (Test-Path $parent) { return $c }
    }
    return $candidates[0]   # nothing found: create the first one and hope
}

$Scripts = Get-ScriptsFolder
$Link    = Join-Path $Scripts "Whisper Subtitles.py"

if ($Uninstall) {
    Remove-Item -Force -ErrorAction SilentlyContinue $Link
    Remove-Item -Force -ErrorAction SilentlyContinue $Marker
    Say "Removed the plugin from Resolve."
    Write-Host ""
    Write-Host "Still on disk, delete them yourself if you want the space back:"
    Write-Host "  Remove-Item -Recurse -Force `"$Repo\.venv`""
    # NOTE: never suggest wiping the whole cache - other tools keep models there too.
    $models = Get-ChildItem -Directory -ErrorAction SilentlyContinue `
        (Join-Path $HOME ".cache\huggingface\hub") | Where-Object Name -like "models--Systran--faster-whisper-*"
    if ($models) {
        Write-Host "  # the Whisper models this plugin downloaded:"
        foreach ($m in $models) { Write-Host "  Remove-Item -Recurse -Force `"$($m.FullName)`"" }
        Write-Host "  # (leave the rest of ~/.cache/huggingface alone: other apps use it)"
    }
    exit 0
}

Say "Whisper Subtitles for DaVinci Resolve"
Write-Host "Repository: $Repo"
Write-Host ""

# --- prerequisites ---------------------------------------------------------------------
# NB: do not call this $args - that is an automatic variable in PowerShell.
$py = $null
$pyPrefix = @()
foreach ($c in @("py", "python", "python3")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $prefix = if ($c -eq "py") { @("-3") } else { @() }
    try {
        $v = & $cmd.Source @prefix "-c" "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch { continue }
    # [version] so that 3.13 compares greater than 3.9; as strings it would not
    if ($v -and [version]$v -ge [version]"3.9") { $py = $cmd.Source; $pyPrefix = $prefix; break }
}
if (-not $py) { Die "Python 3.9 or newer not found. Install it from python.org (tick 'Add to PATH')." }
Write-Host "  python $v"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Warn "  ffmpeg not found - it is required to read the audio."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Die "Install it with:  winget install Gyan.FFmpeg   then run this again."
    }
    Die "Install ffmpeg and put it on PATH, then run this again."
}
Write-Host "  ffmpeg found"

# --- virtualenv ------------------------------------------------------------------------
Write-Host ""
Say "Setting up the virtualenv (this downloads ~150 MB the first time)"
$venv = Join-Path $Repo ".venv"
if (-not (Test-Path $venv)) { & $py @pyPrefix -m venv $venv }
$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Die "The virtualenv was created but $venvPy is missing." }
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r (Join-Path $Repo "requirements.txt")
$fw = & $venvPy -c "import faster_whisper; print(faster_whisper.__version__)"
Write-Host "  faster-whisper $fw"

# --- install into Resolve --------------------------------------------------------------
Write-Host ""
Say "Installing into Resolve"
New-Item -ItemType Directory -Force -Path $Scripts | Out-Null
Remove-Item -Force -ErrorAction SilentlyContinue $Link
$source = Join-Path $Repo "Whisper Subtitles.py"
try {
    # a symlink means "git pull" is enough to update; needs Developer Mode or admin
    New-Item -ItemType SymbolicLink -Path $Link -Target $source -ErrorAction Stop | Out-Null
    Write-Host "  $Link (symlink)"
} catch {
    Copy-Item $source $Link
    Write-Host "  $Link (copy)"
    Warn "  Could not create a symlink, so the plugin was copied."
    Warn "  Re-run this installer after every 'git pull' to update it."
}
Set-Content -Path $Marker -Value $Repo -Encoding UTF8

Write-Host @"

Done. One thing left to do by hand:

  Restart DaVinci Resolve.

Resolve enumerates the Scripts menu at startup, so a freshly installed plugin only
shows up after a restart. Then open it from:

  Workspace > Scripts > Utility > Whisper Subtitles

The first run downloads the Whisper model (~3 GB for large-v3, less for the smaller
ones) and caches it; later runs reuse it. Logs go to $HOME\whisper_subtitles.log
"@
