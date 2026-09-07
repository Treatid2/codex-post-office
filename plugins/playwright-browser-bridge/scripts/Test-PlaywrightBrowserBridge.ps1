# SPDX-License-Identifier: MPL-2.0

[CmdletBinding()]
param(
    [ValidateSet('DedicatedChrome', 'ManagedChrome')]
    [string] $BrowserMode = 'ManagedChrome'
)

$ErrorActionPreference = 'Stop'
$bridge = Join-Path $PSScriptRoot 'Invoke-PlaywrightBrowserBridge.ps1'
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) (
    'post-office-next-playwright-test-' + [Guid]::NewGuid().ToString('N')
)
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()
$started = $false

function Invoke-Bridge([hashtable] $BridgeArguments) {
    $output = & $bridge @BridgeArguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($output -join [Environment]::NewLine) }
    return ($output -join [Environment]::NewLine | ConvertFrom-Json)
}

try {
    $preflight = Invoke-Bridge -BridgeArguments @{
        Command = 'preflight'
        Port = $port
        BrowserMode = $BrowserMode
        StateRoot = $temporaryRoot
    }
    if (-not $preflight.ok -or $preflight.nodeVersion -notmatch '^v(?:2[0-9]|[3-9][0-9])\.') {
        throw 'Preflight did not establish a supported Node.js runtime.'
    }

    $start = Invoke-Bridge -BridgeArguments @{
        Command = 'start'
        Port = $port
        BrowserMode = $BrowserMode
        Headless = $true
        BrowserCheck = $true
        StateRoot = $temporaryRoot
        TimeoutSeconds = 45
    }
    $started = $true
    if (-not $start.ok -or -not $start.probe.browserCheck.ok -or
        -not $start.probe.securityCheck.ok) {
        throw 'Start did not complete the browser-level probe.'
    }

    $status = Invoke-Bridge -BridgeArguments @{
        Command = 'status'
        Port = $port
        BrowserCheck = $true
        StateRoot = $temporaryRoot
    }
    if (-not $status.running -or $status.probe.toolCount -lt 1) {
        throw 'Status did not verify the running MCP endpoint.'
    }

    $stop = Invoke-Bridge -BridgeArguments @{
        Command = 'stop'
        Port = $port
        StateRoot = $temporaryRoot
    }
    $started = $false
    if ($stop.running) { throw 'Stop did not report a stopped bridge.' }
    if ($BrowserMode -eq 'DedicatedChrome' -and
        -not (Get-ChildItem -LiteralPath (Join-Path $temporaryRoot 'chrome-profile') -Force |
            Select-Object -First 1)) {
        throw 'Dedicated Chrome did not retain its profile state.'
    }

    @{
        ok = $true
        browserMode = $BrowserMode
        port = $port
        toolCount = $status.probe.toolCount
        browserCheck = $status.probe.browserCheck
        securityCheck = $status.probe.securityCheck
    } | ConvertTo-Json -Depth 4 -Compress
} finally {
    if ($started) {
        & $bridge stop -Port $port -StateRoot $temporaryRoot -TimeoutSeconds 10 2>$null |
            Out-Null
    }
    Start-Sleep -Milliseconds 300
    $resolvedTemporaryRoot = [IO.Path]::GetFullPath($temporaryRoot)
    $resolvedSystemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if ($resolvedTemporaryRoot.StartsWith(
            $resolvedSystemTemp,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        (Split-Path -Leaf $resolvedTemporaryRoot) -like 'post-office-next-playwright-test-*' -and
        (Test-Path -LiteralPath $resolvedTemporaryRoot)) {
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force
    }
}
