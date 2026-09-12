$ErrorActionPreference = 'Stop'
$serviceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $serviceDir 'story-service-common.ps1')
$paths = Get-RoroStoryServicePaths -ServiceDir $serviceDir
$health = Get-RoroStoryHealth -Port 8877
$listenerPids = @(Get-RoroStoryListenerPids -Port 8877)

if (-not $health -and -not (Test-Path -LiteralPath $paths.PidFile)) {
    Write-Output 'Roro Story Service is not running.'
    exit 0
}

$targets = @()
if ($health -and $listenerPids.Count) {
    $targets = $listenerPids
} elseif (Test-Path -LiteralPath $paths.PidFile) {
    $targets = @([int](Get-Content -Raw -LiteralPath $paths.PidFile))
}

foreach ($servicePid in $targets) {
    $process = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $servicePid
        $process.WaitForExit(5000)
    }
}
Remove-Item -LiteralPath $paths.PidFile -Force -ErrorAction SilentlyContinue
Write-Output 'Roro Story Service stopped.'
