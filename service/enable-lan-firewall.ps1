param([int]$Port = 8877)

$ErrorActionPreference = 'Stop'
$ruleName = "Roro Story Service $Port"
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Enable-NetFirewallRule -DisplayName $ruleName
    Write-Output "Firewall rule already exists and is enabled: $ruleName"
    exit 0
}

New-NetFirewallRule `
    -DisplayName $ruleName `
    -Description 'Allow trusted local-subnet devices to open the Roro Story Studio web service.' `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $Port `
    -Profile Any `
    -RemoteAddress LocalSubnet | Out-Null

Write-Output "Created firewall rule: $ruleName (TCP $Port, LocalSubnet only)"
