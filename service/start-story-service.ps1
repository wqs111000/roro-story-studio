param(
    [string]$HostAddress = '127.0.0.1',
    [int]$Port = 8877,
    [ValidateSet('Development', 'Pin')]
    [string]$ReviewMode = 'Development'
)

$ErrorActionPreference = 'Stop'
$serviceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceDir = Split-Path -Parent $serviceDir
. (Join-Path $serviceDir 'story-service-common.ps1')
$paths = Get-RoroStoryServicePaths -ServiceDir $serviceDir
$serverScript = Join-Path $serviceDir 'story_server.py'
$approvedDir = Join-Path $workspaceDir 'approved'
$requestedReviewMode = $ReviewMode.ToLowerInvariant()

New-Item -ItemType Directory -Force -Path $paths.RuntimeDir | Out-Null
if ($ReviewMode -eq 'Pin' -and -not (Test-Path -LiteralPath $paths.ParentPinFile)) {
    $parentPin = [System.Security.Cryptography.RandomNumberGenerator]::GetInt32(100000, 1000000)
    Set-Content -LiteralPath $paths.ParentPinFile -Value $parentPin -Encoding ascii
}

$health = Get-RoroStoryHealth -Port $Port
if ($health) {
    if ($health.review_mode -ne $requestedReviewMode) {
        throw "Service is already running in review mode '$($health.review_mode)'. Stop it before switching to '$requestedReviewMode'."
    }
    $listeners = @(Get-RoroStoryListenerPids -Port $Port)
    if ($listeners.Count -eq 1) {
        Set-Content -LiteralPath $paths.PidFile -Value $listeners[0] -Encoding ascii
    }
    Write-Output "Roro Story Service is already running (PID $($listeners -join ', '))."
    Write-Output "Review mode: $($health.review_mode)"
    if ($health.review_mode -eq 'pin') {
        Write-Output "Parent review PIN: $((Get-Content -Raw -LiteralPath $paths.ParentPinFile).Trim())"
    }
    Write-RoroStoryUrls -Port $Port
    exit 0
}

$occupied = @(Get-RoroStoryListenerPids -Port $Port)
if ($occupied.Count) {
    throw "Port $Port is already occupied by PID $($occupied -join ', '), but it is not Roro Story Service."
}

if (Test-Path -LiteralPath $paths.PidFile) {
    Remove-Item -LiteralPath $paths.PidFile -Force
}

$python = Get-Command python -ErrorAction Stop
$arguments = @(
    $serverScript,
    '--host', $HostAddress,
    '--port', $Port,
    '--directory', $approvedDir,
    '--pin-file', $paths.ParentPinFile,
    '--review-auth-mode', $requestedReviewMode
)
$process = Start-Process -FilePath $python.Source -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput $paths.StdoutLog -RedirectStandardError $paths.StderrLog

$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 250
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 1
        if ($health.status -eq 'ok' -and $health.service -eq 'roro-story-service') {
            $listeners = @(Get-RoroStoryListenerPids -Port $Port)
            if ($listeners.Count -ne 1) {
                throw "Expected one listener on port $Port, found $($listeners.Count)."
            }
            Set-Content -LiteralPath $paths.PidFile -Value $listeners[0] -Encoding ascii
            Write-Output "Roro Story Service started (PID $($listeners[0]))."
            Write-Output "Review mode: $($health.review_mode)"
            if ($health.review_mode -eq 'pin') {
                Write-Output "Parent review PIN: $((Get-Content -Raw -LiteralPath $paths.ParentPinFile).Trim())"
            }
            Write-RoroStoryUrls -Port $Port
            exit 0
        }
    } catch {
        # The service can take several seconds to import and scan local assets.
    }
} while ((Get-Date) -lt $deadline)

if (-not $process.HasExited) {
    Stop-Process -Id $process.Id -Force
}
Remove-Item -LiteralPath $paths.PidFile -Force -ErrorAction SilentlyContinue
throw "Roro Story Service did not become healthy. See $($paths.StderrLog)"
