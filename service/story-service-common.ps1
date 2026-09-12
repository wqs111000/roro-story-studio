function Get-RoroStoryServicePaths {
    param([Parameter(Mandatory)][string]$ServiceDir)

    $runtimeDir = Join-Path $ServiceDir '.runtime'
    [pscustomobject]@{
        RuntimeDir = $runtimeDir
        PidFile = Join-Path $runtimeDir 'story-service.pid'
        StdoutLog = Join-Path $runtimeDir 'story-service.out.log'
        StderrLog = Join-Path $runtimeDir 'story-service.err.log'
        ParentPinFile = Join-Path $runtimeDir 'parent-pin.txt'
    }
}

function Get-RoroStoryLanAddresses {
    $addresses = [System.Collections.Generic.List[string]]::new()
    foreach ($networkInterface in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        if ($networkInterface.OperationalStatus -ne 'Up') { continue }
        if ($networkInterface.NetworkInterfaceType -in @('Loopback', 'Tunnel')) { continue }
        $interfaceLabel = "$($networkInterface.Name) $($networkInterface.Description)"
        if ($interfaceLabel -match 'vEthernet|WSL|Hyper-V|Default Switch|NodeBabyLink|VirtualBox|VMware|Bluetooth') { continue }
        foreach ($unicast in $networkInterface.GetIPProperties().UnicastAddresses) {
            $address = $unicast.Address
            if ($address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { continue }
            $text = $address.ToString()
            if ([System.Net.IPAddress]::IsLoopback($address) -or $text -like '169.254.*') { continue }
            $addresses.Add($text)
        }
    }
    return @($addresses | Sort-Object -Unique)
}

function Get-RoroStoryListenerPids {
    param([Parameter(Mandatory)][int]$Port)

    $pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    $pids = foreach ($line in (& netstat -ano -p TCP)) {
        if ($line -match $pattern) { [int]$Matches[1] }
    }
    return @($pids | Sort-Object -Unique)
}

function Get-RoroStoryHealth {
    param([Parameter(Mandatory)][int]$Port)

    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($health.status -eq 'ok' -and $health.service -eq 'roro-story-service') { return $health }
    } catch {
        return $null
    }
    return $null
}

function Write-RoroStoryUrls {
    param([Parameter(Mandatory)][int]$Port)

    Write-Output "Local URL: http://127.0.0.1:$Port/"
    foreach ($address in Get-RoroStoryLanAddresses) {
        Write-Output "LAN URL: http://${address}:$Port/"
    }
}
