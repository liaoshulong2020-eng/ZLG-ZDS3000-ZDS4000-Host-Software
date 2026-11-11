$ErrorActionPreference = 'Stop'
$resultPath = Join-Path $PSScriptRoot 'data\network_diagnostics\repair_result.json'
try {
    $adapter = @(Get-NetAdapter | Where-Object MacAddress -eq '20-7B-D5-1A-3B-74')
    if ($adapter.Count -ne 1 -or $adapter[0].Status -ne 'Up') {
        throw 'USB Ethernet link is not up; no configuration changed.'
    }
    $index = $adapter[0].ifIndex
    $addresses = @(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4)
    $unrelated = @($addresses | Where-Object {
        $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '192.168.1.200'
    })
    if ($unrelated.Count) { throw 'Ethernet already has another static address; no configuration changed.' }
    if (-not ($addresses | Where-Object IPAddress -eq '192.168.1.200')) {
        New-NetIPAddress -InterfaceIndex $index -IPAddress '192.168.1.200' -PrefixLength 24 | Out-Null
    }
    $active = @(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 |
        Select-Object IPAddress,PrefixLength,AddressState)
    $persistent = @(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -PolicyStore PersistentStore |
        Select-Object IPAddress,PrefixLength)
    @{success=$true;interface=$adapter[0].Name;active=$active;persistent=$persistent} |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $resultPath -Encoding UTF8
} catch {
    @{success=$false;error=$_.Exception.Message} |
        ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    exit 1
}
