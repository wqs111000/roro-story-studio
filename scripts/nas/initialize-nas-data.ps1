param(
    [Parameter(Mandatory)][string]$NasRoot
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = [System.IO.Path]::GetFullPath($NasRoot)
if ([System.IO.Path]::GetPathRoot($resolvedRoot) -eq $resolvedRoot) {
    throw 'NasRoot must be a dedicated directory, not a drive root.'
}

$dataRoot = Join-Path $resolvedRoot 'data'
$runtimeRoot = Join-Path $resolvedRoot 'runtime'
foreach ($path in @(
    $dataRoot,
    (Join-Path $dataRoot 'incoming'),
    (Join-Path $dataRoot 'drafts'),
    (Join-Path $dataRoot 'approved'),
    (Join-Path $dataRoot 'exports'),
    (Join-Path $resolvedRoot 'service-data'),
    (Join-Path $resolvedRoot 'character-assets'),
    (Join-Path $resolvedRoot 'backup'),
    $runtimeRoot
)) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

$pinPath = Join-Path $runtimeRoot 'parent-pin.txt'
if (-not (Test-Path -LiteralPath $pinPath)) {
    $pin = [System.Security.Cryptography.RandomNumberGenerator]::GetInt32(100000, 1000000)
    Set-Content -LiteralPath $pinPath -Value $pin -Encoding ascii
    Write-Output "Parent PIN created: $pin"
} else {
    Write-Output 'Parent PIN already exists and was preserved.'
}
Write-Output "NAS data root initialized: $resolvedRoot"
