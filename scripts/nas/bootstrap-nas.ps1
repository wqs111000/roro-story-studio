param(
    [Parameter(Mandatory)][string]$NasRoot,
    [string]$NasLinuxRoot = '/volume1/docker/roro-story-studio',
    [string]$WorkspaceRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$Version = ('nas-' + (Get-Date -Format 'yyyyMMdd-HHmmss')),
    [string]$ListenAddress = '127.0.0.1',
    [string]$DesktopUrl = '',
    [string]$RuntimeImage = 'roro-story-runtime:py312-reportlab4-v1'
)

# Display-only preparation: current shelf releases, no drafts/history or service start.
$ErrorActionPreference = 'Stop'
if ($Version -notmatch '^nas-[a-z0-9][a-z0-9._-]*$') { throw 'Invalid version tag.' }
$workspace = (Resolve-Path -LiteralPath $WorkspaceRoot).ProviderPath
$nas = (Resolve-Path -LiteralPath $NasRoot).ProviderPath
$image = $RuntimeImage
$localOutput = Join-Path $workspace "output/nas/$Version"
if (Test-Path -LiteralPath $localOutput) { throw 'Local output already exists; use a new Version.' }
$destination = Join-Path (Join-Path $nas 'staging') $Version
if (Test-Path -LiteralPath $destination) { throw 'NAS staging version already exists; use a new Version.' }
New-Item -ItemType Directory -Path $localOutput -Force | Out-Null

& docker image inspect $image *> $null
if ($LASTEXITCODE -ne 0) {
    & docker build --platform linux/amd64 --file (Join-Path $workspace 'deploy/nas/Dockerfile.runtime') --tag $image $workspace
    if ($LASTEXITCODE -ne 0) { throw 'Runtime image build failed. No NAS data was changed.' }
}
$imageInfo = (& docker image inspect $image | ConvertFrom-Json)[0]
if ($LASTEXITCODE -ne 0 -or $imageInfo.Os -ne 'linux' -or $imageInfo.Architecture -ne 'amd64') {
    throw 'Expected a Linux amd64 image.'
}
$archive = Join-Path $localOutput 'image.tar'
& docker image save --output $archive $image
if ($LASTEXITCODE -ne 0) { throw 'Image export failed.' }

& python -X utf8 (Join-Path $PSScriptRoot 'prepare_release.py') prepare `
    --workspace $workspace --destination $destination `
    --linux-path "$($NasLinuxRoot.TrimEnd('/'))/staging/$Version" `
    --image $image --image-id $imageInfo.Id --image-archive $archive --listen-address $ListenAddress --desktop-url $DesktopUrl
if ($LASTEXITCODE -ne 0) { throw 'Preparation failed; incomplete staging is retained for diagnosis.' }
Write-Output "Prepared (NOT started): $destination"
Write-Output 'Read START-HERE.md, then import compose.yaml in the UGREEN Docker UI. Import image.tar only when the NAS runtime image is missing or dependencies changed.'
