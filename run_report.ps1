param(
    [switch]$NoPause,
    [string]$LogPath
)

# NOTE: keep this script ASCII-only. Windows PowerShell 5.1 reads a BOM-less .ps1
# using the system ANSI code page, so non-ASCII characters here can break parsing.
# Do NOT use "Stop": PS 5.1 turns a native program's (python) stderr writes into
# error records, which under "Stop" would abort the whole script mid-run.
$ErrorActionPreference = "Continue"
# Make Python emit UTF-8 so the log stays readable when run without a console.
$env:PYTHONIOENCODING = "utf-8"
# PowerShell decodes a native program's output with the console output encoding,
# not PYTHONIOENCODING. Force UTF-8 so captured Chinese is not mojibake. Wrapped
# in try/catch because no console may be attached under Task Scheduler.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPath = Join-Path $projectDir "youtube_viral_radar.py"

function Write-Status {
    param([string]$Message, [string]$Color = "Gray")
    if ($LogPath) {
        ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) |
            Out-File -FilePath $LogPath -Encoding utf8 -Append
    }
    else {
        Write-Host $Message -ForegroundColor $Color
    }
}

# Start each scheduled run with a fresh log (latest run only).
if ($LogPath) {
    ("[{0}] Report run started" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss")) |
        Out-File -FilePath $LogPath -Encoding utf8
}

$candidates = @(
    @{ File = "python"; Prefix = @() },
    @{ File = "py"; Prefix = @("-3") },
    @{
        File = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
        Prefix = @()
    }
)

$python = $null
foreach ($candidate in $candidates) {
    try {
        & $candidate.File @($candidate.Prefix) --version *> $null
        if ($LASTEXITCODE -eq 0) {
            $python = $candidate
            break
        }
    }
    catch {
        continue
    }
}

if (-not $python) {
    Write-Status "Python 3 was not found. Install Python and enable Add Python to PATH." "Red"
    $exitCode = 1
}
else {
    Write-Status "Generating the YouTube viral radar report..." "Cyan"
    Push-Location $projectDir
    try {
        if ($LogPath) {
            # 2>&1 folds stderr into the pipeline; unwrap error records to plain
            # text so the log is clean (no NativeCommandError decoration).
            & $python.File @($python.Prefix) $scriptPath 2>&1 |
                ForEach-Object {
                    if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message }
                    else { "$_" }
                } |
                Out-File -FilePath $LogPath -Encoding utf8 -Append
            $exitCode = $LASTEXITCODE
        }
        else {
            & $python.File @($python.Prefix) $scriptPath
            $exitCode = $LASTEXITCODE
        }
    }
    finally {
        Pop-Location
    }

    if ($exitCode -eq 0) {
        Write-Status "Report generated successfully." "Green"
    }
    else {
        Write-Status "Report generation failed (exit $exitCode). Review the output above." "Red"
    }
}

if (-not $NoPause) {
    Write-Host ""
    Read-Host "Press Enter to close"
}

exit $exitCode
