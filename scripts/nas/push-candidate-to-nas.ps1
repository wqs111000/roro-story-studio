param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9._-]+$')][string]$StoryId,
    [Parameter(Mandatory)][string]$NasDataRoot,
    [string]$WorkspaceRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)

$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath $WorkspaceRoot).Path
$nasRoot = (Resolve-Path -LiteralPath $NasDataRoot).Path
$syncScript = Join-Path $PSScriptRoot 'candidate_sync.py'
$python = Get-Command python -ErrorAction Stop

& $python.Source $syncScript push $StoryId --workspace $workspace --nas-data-root $nasRoot
if ($LASTEXITCODE -ne 0) {
    throw "Candidate push failed with exit code $LASTEXITCODE."
}
