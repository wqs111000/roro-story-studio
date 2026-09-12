param([int]$Port = 8877)

$ErrorActionPreference = 'Stop'
$serviceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $serviceDir 'story-service-common.ps1')
$paths = Get-RoroStoryServicePaths -ServiceDir $serviceDir

try {
    $health = Get-RoroStoryHealth -Port $Port
    if (-not $health) { throw 'unhealthy' }
    $stories = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/stories" -TimeoutSec 2
    $listeners = @(Get-RoroStoryListenerPids -Port $Port)
    if ($listeners.Count -eq 1) {
        New-Item -ItemType Directory -Force -Path $paths.RuntimeDir | Out-Null
        Set-Content -LiteralPath $paths.PidFile -Value $listeners[0] -Encoding ascii
    }
    Write-Output "Status: $($health.status)"
    Write-Output "PID: $($listeners -join ', ')"
    $bindings = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalAddress -Unique)
    Write-Output "Bind: $($bindings -join ', ')"
    Write-Output "Stories: $($stories.stories.Count)"
    Write-Output "Review mode: $($health.review_mode)"
    if ($health.review_mode -eq 'pin' -and (Test-Path -LiteralPath $paths.ParentPinFile)) {
        Write-Output "Parent review PIN: $((Get-Content -Raw -LiteralPath $paths.ParentPinFile).Trim())"
    }
    Write-RoroStoryUrls -Port $Port
    exit 0
} catch {
    Write-Output 'Status: stopped or unhealthy'
    if (Test-Path -LiteralPath $paths.PidFile) {
        Write-Output "Stale PID file: $(Get-Content -Raw -LiteralPath $paths.PidFile)"
    }
    exit 1
}
