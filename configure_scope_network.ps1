$ErrorActionPreference = 'Stop'
try {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Start-Process powershell.exe -Verb RunAs -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'))
        exit
    }
    # Only configure the physical USB Ethernet adapter observed on this PC.
    $adapter = Get-NetAdapter | Where-Object { $_.MacAddress -eq '20-7B-D5-1A-3B-74' }
    if (@($adapter).Count -ne 1 -or $adapter.Status -ne 'Up') {
        throw 'Expected USB Ethernet adapter is missing or disconnected. No settings changed.'
    }
    $addresses = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4)
    $unrelated = @($addresses | Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '192.168.1.200' })
    if ($unrelated.Count) {
        throw 'Ethernet already has another IPv4 configuration. No settings changed.'
    }
    $backupPath = Join-Path $PSScriptRoot ('scope_network_before_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.json')
    [pscustomobject]@{
        Adapter = $adapter.Name
        MacAddress = $adapter.MacAddress
        IPv4 = @($addresses | Select-Object IPAddress,PrefixLength,PrefixOrigin)
        Interface = @(Get-NetIPInterface -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 | Select-Object Dhcp,InterfaceMetric)
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $backupPath -Encoding UTF8
    if (-not ($addresses | Where-Object { $_.IPAddress -eq '192.168.1.200' })) {
        New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress '192.168.1.200' -PrefixLength 24 -PolicyStore ActiveStore | Out-Null
    }
    Write-Host 'Ethernet IPv4: 192.168.1.200 / 255.255.255.0. No gateway added.'
    Write-Host ('Previous configuration: ' + $backupPath)
    Write-Host 'This address is configured in ActiveStore. Verify again after restarting Windows.'
    Start-Sleep -Seconds 3
    & ping.exe -n 2 -w 1000 192.168.1.100
    Test-NetConnection -ComputerName 192.168.1.100 -Port 5025 -InformationLevel Detailed
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
} finally {
    Read-Host 'Press Enter to close'
}
