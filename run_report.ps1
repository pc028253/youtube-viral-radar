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

function Invoke-Logged {
    # Run a scriptblock, routing all output to the log (clean UTF-8) or console.
    param([scriptblock]$Action)
    if ($LogPath) {
        & $Action 2>&1 |
            ForEach-Object {
                if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message }
                else { "$_" }
            } |
            Out-File -FilePath $LogPath -Encoding utf8 -Append
    }
    else {
        & $Action
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
        Invoke-Logged { & $python.File @($python.Prefix) $scriptPath }
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($exitCode -eq 0) {
        Write-Status "Report generated successfully." "Green"
        Write-Status "Building mobile site (build_site.py)..." "Cyan"
        Invoke-Logged { & $python.File @($python.Prefix) (Join-Path $projectDir "build_site.py") }

        if (Test-Path (Join-Path $projectDir ".git")) {
            Write-Status "Publishing to GitHub..." "Cyan"
            Invoke-Logged {
                git -C $projectDir add -A
                git -C $projectDir commit -m ("Update report " + (Get-Date -Format "yyyy-MM-dd"))
                git -C $projectDir push
            }
            if ($LASTEXITCODE -eq 0) {
                Write-Status "Published to GitHub Pages." "Green"
            }
            else {
                Write-Status "Publish finished with warnings (no remote/auth yet, or nothing new to push)." "Yellow"
            }
        }
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
