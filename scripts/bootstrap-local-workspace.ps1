$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$copies = @(
    @('profile/youyou.example.md', 'profile/youyou.md'),
    @('profile/roro.example.md', 'profile/roro.md'),
    @('profile/family-rules.example.md', 'profile/family-rules.md'),
    @('universe/character-registry.example.md', 'universe/character-registry.md'),
    @('service/data/characters.example.json', 'service/data/characters.json'),
    @('service/data/story-production-times.example.json', 'service/data/story-production-times.json')
)

foreach ($pair in $copies) {
    $source = Join-Path $workspace $pair[0]
    $destination = Join-Path $workspace $pair[1]
    if (-not (Test-Path -LiteralPath $destination)) {
        Copy-Item -LiteralPath $source -Destination $destination
        Write-Output "Created local file: $($pair[1])"
    } else {
        Write-Output "Kept existing local file: $($pair[1])"
    }
}

foreach ($directory in @('inbox', 'drafts', 'approved', 'exports', 'output', 'tmp')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $workspace $directory) | Out-Null
}

Write-Output 'Local Roro Story Studio workspace is ready.'
