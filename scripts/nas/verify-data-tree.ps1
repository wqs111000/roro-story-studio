param(
    [Parameter(Mandatory)][string]$SourceRoot,
    [Parameter(Mandatory)][string]$DestinationRoot
)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $SourceRoot).Path.TrimEnd('\', '/')
$destination = (Resolve-Path -LiteralPath $DestinationRoot).Path.TrimEnd('\', '/')
if ($source -eq $destination) {
    throw 'SourceRoot and DestinationRoot must be different directories.'
}

function Get-TreeInventory {
    param([Parameter(Mandatory)][string]$Root)
    $inventory = @{}
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File) {
        $relative = $file.FullName.Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
        $inventory[$relative] = [pscustomobject]@{
            Length = $file.Length
            Hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    return $inventory
}

$sourceInventory = Get-TreeInventory -Root $source
$destinationInventory = Get-TreeInventory -Root $destination
$allPaths = @($sourceInventory.Keys + $destinationInventory.Keys | Sort-Object -Unique)
$mismatches = foreach ($relative in $allPaths) {
    $left = $sourceInventory[$relative]
    $right = $destinationInventory[$relative]
    if (-not $left) {
        [pscustomobject]@{ Path = $relative; Problem = 'destination-only' }
    } elseif (-not $right) {
        [pscustomobject]@{ Path = $relative; Problem = 'missing-at-destination' }
    } elseif ($left.Length -ne $right.Length) {
        [pscustomobject]@{ Path = $relative; Problem = 'size-mismatch' }
    } elseif ($left.Hash -ne $right.Hash) {
        [pscustomobject]@{ Path = $relative; Problem = 'hash-mismatch' }
    }
}

if ($mismatches) {
    $mismatches | Format-Table -AutoSize
    throw "Tree verification failed with $($mismatches.Count) mismatch(es)."
}

$totalBytes = ($sourceInventory.Values | Measure-Object -Property Length -Sum).Sum
Write-Output "Tree verification passed: $($sourceInventory.Count) files, $totalBytes bytes."
