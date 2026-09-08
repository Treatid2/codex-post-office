# SPDX-License-Identifier: MPL-2.0

[CmdletBinding()]
param(
    [ValidateSet('DedicatedChrome', 'ManagedChrome')]
    [string] $BrowserMode = 'ManagedChrome'
)

$ErrorActionPreference = 'Stop'
$bridge = Join-Path $PSScriptRoot 'Invoke-PlaywrightBrowserBridge.ps1'
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) (
    'post-office-next-playwright-test-' + [Guid]::NewGuid().ToString('N') + ' with spaces'
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
function Get-TestPort {
    $socket = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try { $socket.Start(); return ([Net.IPEndPoint]$socket.LocalEndpoint).Port }
    finally { $socket.Stop() }
}

try {
    New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null
    $protectedRoot = Join-Path $temporaryRoot 'protected-post-office-state'
    New-Item -ItemType Directory -Path $protectedRoot | Out-Null
    $previousHubRoot = $env:CODEX_COMMS_HUB_ROOT
    try {
        $env:CODEX_COMMS_HUB_ROOT = $protectedRoot
        $unsafeStateRoot = Join-Path $protectedRoot 'browser-bridge'
        $unsafeOutput = & $bridge start -Port $port -BrowserMode $BrowserMode -Headless `
            -StateRoot $unsafeStateRoot -TimeoutSeconds 10 2>&1
        if ($LASTEXITCODE -eq 0) { throw 'Start accepted a StateRoot inside protected Post Office state.' }
        if (Test-Path -LiteralPath $unsafeStateRoot) {
            throw 'Protected StateRoot rejection occurred after bridge state was created.'
        }
    } finally {
        $env:CODEX_COMMS_HUB_ROOT = $previousHubRoot
    }

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
    if ($start.bridge.PSObject.Properties.Name -contains 'brokerShutdownToken') {
        throw 'Start exposed the private shutdown credential.'
    }

    $duplicateOutput = & $bridge start -Port $port -BrowserMode $BrowserMode -Headless `
        -StateRoot $temporaryRoot -TimeoutSeconds 10 2>&1
    if ($LASTEXITCODE -eq 0) { throw 'A repeated start unexpectedly replaced the live bridge.' }
    if (-not (Test-Path -LiteralPath (Join-Path $temporaryRoot 'bridge.json') -PathType Leaf)) {
        throw 'A repeated start removed the live bridge metadata.'
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
    if ($status.bridge.PSObject.Properties.Name -contains 'brokerShutdownToken' -or
        $status.processes.broker.PSObject.Properties.Name -contains 'commandLine' -or
        $status.processes.playwright.PSObject.Properties.Name -contains 'commandLine') {
        throw 'Status exposed private process or shutdown details.'
    }

    # Stop must participate in the same lifecycle lock as start. Holding that
    # exact mutex makes stop fail closed without touching the live metadata.
    $mutexName = 'Local\PostOfficeNext-PlaywrightBridge-' + [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes(
            [IO.Path]::GetFullPath($temporaryRoot).ToLowerInvariant()
        ))
    )
    $heldMutex = [Threading.Mutex]::new($false, $mutexName)
    $held = $heldMutex.WaitOne(0)
    if (-not $held) { throw 'Test could not acquire the bridge lifecycle mutex.' }
    try {
        $pwshForLockedStop = (Get-Command pwsh).Source
        $escapedBridgeForStop = $bridge.Replace("'", "''")
        $escapedRootForStop = $temporaryRoot.Replace("'", "''")
        $lockedStopCommand = "& '$escapedBridgeForStop' stop -Port $port -StateRoot '$escapedRootForStop' -TimeoutSeconds 10; exit `$LASTEXITCODE"
        $lockedStopEncoded = [Convert]::ToBase64String(
            [Text.Encoding]::Unicode.GetBytes($lockedStopCommand)
        )
        $lockedStop = Start-Process -FilePath $pwshForLockedStop `
            -ArgumentList @('-NoProfile', '-EncodedCommand', $lockedStopEncoded) `
            -RedirectStandardOutput (Join-Path $temporaryRoot 'locked-stop.stdout.log') `
            -RedirectStandardError (Join-Path $temporaryRoot 'locked-stop.stderr.log') `
            -WindowStyle Hidden -PassThru
        $lockedStop.WaitForExit()
        if ($lockedStop.ExitCode -eq 0) { throw 'Stop ignored the active lifecycle mutex.' }
        if (-not (Test-Path -LiteralPath (Join-Path $temporaryRoot 'bridge.json') -PathType Leaf)) {
            throw 'Mutex-refused stop removed live bridge metadata.'
        }
    } finally {
        $heldMutex.ReleaseMutex()
        $heldMutex.Dispose()
    }
    $null = Invoke-Bridge -BridgeArguments @{
        Command = 'status'; Port = $port; StateRoot = $temporaryRoot
    }

    $metadataPath = Join-Path $temporaryRoot 'bridge.json'
    $metadataText = Get-Content -Raw -LiteralPath $metadataPath
    $tampered = $metadataText | ConvertFrom-Json
    $tampered.playwrightCreationUtcTicks = [long]$tampered.playwrightCreationUtcTicks + 1
    $tampered | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $metadataPath -Encoding utf8NoBOM
    $mismatchOutput = & $bridge stop -Port $port -StateRoot $temporaryRoot -TimeoutSeconds 10 2>&1
    if ($LASTEXITCODE -eq 0) { throw 'Stop accepted a changed process creation identity.' }
    if (-not (Get-Process -Id ([int]$tampered.playwrightProcessId) -ErrorAction SilentlyContinue)) {
        throw 'Identity-mismatch stop killed the process it was required to preserve.'
    }
    Set-Content -LiteralPath $metadataPath -Value $metadataText -Encoding utf8NoBOM -NoNewline

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

    # Launch two independent wrappers together. Exactly one may publish the
    # state record; the other must fail without deleting it.
    $pwsh = (Get-Command pwsh).Source
    $escapedBridge = $bridge.Replace("'", "''")
    $escapedRoot = $temporaryRoot.Replace("'", "''")
    $concurrentCommand = "& '$escapedBridge' start -Port $port -BrowserMode $BrowserMode -Headless -StateRoot '$escapedRoot' -TimeoutSeconds 45; exit `$LASTEXITCODE"
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($concurrentCommand))
    $concurrent = foreach ($ordinal in 1..2) {
        Start-Process -FilePath $pwsh -ArgumentList @('-NoProfile', '-EncodedCommand', $encodedCommand) `
            -RedirectStandardOutput (Join-Path $temporaryRoot "concurrent-$ordinal.stdout.log") `
            -RedirectStandardError (Join-Path $temporaryRoot "concurrent-$ordinal.stderr.log") `
            -WindowStyle Hidden -PassThru
    }
    $concurrent | ForEach-Object { $_.WaitForExit() }
    $exitCodes = @($concurrent | ForEach-Object { $_.ExitCode } | Sort-Object)
    if ($exitCodes.Count -ne 2 -or $exitCodes[0] -ne 0 -or $exitCodes[1] -eq 0) {
        throw "Concurrent start did not yield exactly one owner: $($exitCodes -join ',')"
    }
    $started = $true
    $null = Invoke-Bridge -BridgeArguments @{ Command = 'status'; Port = $port; StateRoot = $temporaryRoot }
    $null = Invoke-Bridge -BridgeArguments @{ Command = 'stop'; Port = $port; StateRoot = $temporaryRoot }
    $started = $false

    # Simulate an interrupted STARTING record with only the exact Playwright
    # listener alive. The next start must recover that component and proceed.
    $rawPort = Get-TestPort
    $node = (Get-Command node).Source
    $stubScript = Join-Path $PSScriptRoot 'playwright-mcp-test-stub.mjs'
    $stub = Start-Process -FilePath $node -ArgumentList @($stubScript, '--port', [string]$rawPort) `
        -WindowStyle Hidden -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        $stubListener = Get-NetTCPConnection -State Listen -LocalPort $rawPort -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -eq '127.0.0.1' } | Select-Object -First 1
        if (-not $stubListener) { Start-Sleep -Milliseconds 100 }
    } while (-not $stubListener -and [DateTime]::UtcNow -lt $deadline)
    if (-not $stubListener) { throw 'Partial-pair test stub did not bind.' }
    $stubCim = Get-CimInstance Win32_Process -Filter "ProcessId = $($stubListener.OwningProcess)"
    $stubCreated = if ($stubCim.CreationDate -is [DateTime]) {
        $stubCim.CreationDate.ToUniversalTime()
    } else {
        [Management.ManagementDateTimeConverter]::ToDateTime([string]$stubCim.CreationDate).ToUniversalTime()
    }
    [ordered]@{
        schemaVersion = 3; state = 'STARTING'; startAttemptId = 'interrupted-test'
        endpoint = "http://127.0.0.1:$port/mcp"; host = '127.0.0.1'; port = $port
        internalEndpoint = "http://127.0.0.1:$rawPort/mcp"
        playwrightProcessId = [int]$stubListener.OwningProcess; playwrightPort = $rawPort
        playwrightCreationDate = $stubCreated.ToString('o'); playwrightCreationUtcTicks = $stubCreated.Ticks
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $temporaryRoot 'bridge.json') -Encoding utf8NoBOM
    $null = Invoke-Bridge -BridgeArguments @{
        Command = 'start'; Port = $port; BrowserMode = $BrowserMode; Headless = $true
        StateRoot = $temporaryRoot; TimeoutSeconds = 45
    }
    $started = $true
    $stub.Refresh()
    if (-not $stub.HasExited) { throw 'Partial-pair recovery left the prior exact listener alive.' }
    $null = Invoke-Bridge -BridgeArguments @{ Command = 'stop'; Port = $port; StateRoot = $temporaryRoot }
    $started = $false

    @{
        ok = $true
        browserMode = $BrowserMode
        port = $port
        toolCount = $status.probe.toolCount
        browserCheck = $status.probe.browserCheck
        securityCheck = $status.probe.securityCheck
        repeatedStartPreservedService = $true
        concurrentStartSingleOwner = $true
        partialPairRecovered = $true
        changedIdentityRefused = $true
        protectedStateRootRejectedBeforeWrite = $true
        stopSerializedWithStart = $true
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
