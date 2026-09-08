# SPDX-License-Identifier: MPL-2.0

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('preflight', 'start', 'bootstrap', 'deliver-message', 'collect-attachment', 'status', 'probe', 'stop')]
    [string] $Command,

    [ValidateRange(1024, 65535)]
    [int] $Port = 8931,

    [ValidateSet('DedicatedChrome', 'ExistingChrome', 'ManagedChrome')]
    [string] $BrowserMode = 'DedicatedChrome',

    [switch] $Headless,
    [switch] $BrowserCheck,

    [ValidateRange(1, 120)]
    [int] $TimeoutSeconds = 30,

    [string] $StateRoot = (Join-Path $env:LOCALAPPDATA 'Codex\PostOfficeNext\playwright-browser-bridge'),

    [string] $ManifestPath
)

$ErrorActionPreference = 'Stop'
$packageVersion = '0.0.80'
$bindHost = '127.0.0.1'
$endpoint = "http://${bindHost}:$Port/mcp"
$metadataPath = Join-Path $StateRoot 'bridge.json'
$probeScript = Join-Path $PSScriptRoot 'playwright_bridge_probe.mjs'
$bootstrapScript = Join-Path $PSScriptRoot 'playwright_chatgpt_bootstrap.mjs'
$collectorScript = Join-Path $PSScriptRoot 'playwright_chatgpt_collect.mjs'
$directCollectorScript = Join-Path $PSScriptRoot 'playwright_chatgpt_collect_direct.mjs'
$deliveryScript = Join-Path $PSScriptRoot 'playwright_chatgpt_deliver.mjs'
$brokerScript = Join-Path $PSScriptRoot 'playwright_mcp_broker.mjs'
$profileRoot = Join-Path $StateRoot 'chrome-profile'
$playwrightCoreVersion = '1.63.0-alpha-2026-08-31'
$startAttemptId = if ($Command -eq 'start') { [Guid]::NewGuid().ToString('N') } else { $null }
$metadataOwnedByThisStart = $false
$stateMutex = $null
$stateMutexOwned = $false

function Get-PhysicalPathKey([string] $Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $existing = $full
    $suffix = [Collections.Generic.List[string]]::new()
    while (-not (Test-Path -LiteralPath $existing)) {
        $leaf = Split-Path -Leaf $existing
        if (-not $leaf) { break }
        $suffix.Insert(0, $leaf)
        $existing = Split-Path -Parent $existing
    }
    if (-not (Test-Path -LiteralPath $existing -PathType Container)) {
        throw "StateRoot has no existing directory ancestor: $full"
    }
    $resolved = (Resolve-Path -LiteralPath $existing).ProviderPath
    $cursor = Get-Item -LiteralPath $resolved -Force
    if ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "StateRoot topology uses a reparse point: $($cursor.FullName)"
    }
    foreach ($part in $suffix) { $resolved = Join-Path $resolved $part }
    return [IO.Path]::GetFullPath($resolved).TrimEnd('\').ToLowerInvariant()
}

function Test-PathWithin([string] $Candidate, [string] $Root) {
    return $Candidate -eq $Root -or $Candidate.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Assert-SafeStateRoot {
    $stateKey = Get-PhysicalPathKey $StateRoot
    $protected = [Collections.Generic.List[string]]::new()
    if ($env:CODEX_COMMS_HUB_ROOT) { $protected.Add($env:CODEX_COMMS_HUB_ROOT) }
    if ($env:POST_OFFICE_STATE_ROOT) { $protected.Add($env:POST_OFFICE_STATE_ROOT) }
    foreach ($root in $protected) {
        $rootKey = Get-PhysicalPathKey $root
        if ((Test-PathWithin $stateKey $rootKey) -or (Test-PathWithin $rootKey $stateKey)) {
            throw "Browser bridge StateRoot overlaps protected Post Office state: $root"
        }
    }
}

function Write-Result([hashtable] $Value) {
    $Value | ConvertTo-Json -Depth 8 -Compress
}

function ConvertTo-NativeArgument([string] $Value) {
    if ($Value -notmatch '[\s"]') { return $Value }
    $builder = [Text.StringBuilder]::new()
    $null = $builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            $null = $builder.Append(('\' * (($backslashes * 2) + 1)))
            $null = $builder.Append('"')
        } else {
            if ($backslashes) { $null = $builder.Append(('\' * $backslashes)) }
            $null = $builder.Append($character)
        }
        $backslashes = 0
    }
    if ($backslashes) { $null = $builder.Append(('\' * ($backslashes * 2))) }
    $null = $builder.Append('"')
    return $builder.ToString()
}

function Get-PublicMetadata($Metadata) {
    $public = [ordered]@{}
    if ($Metadata -is [Collections.IDictionary]) {
        foreach ($key in $Metadata.Keys) {
            if ($key -notin @('brokerShutdownToken', 'startAttemptId')) { $public[$key] = $Metadata[$key] }
        }
    } else {
        foreach ($property in $Metadata.PSObject.Properties) {
            if ($property.Name -notin @('brokerShutdownToken', 'startAttemptId')) { $public[$property.Name] = $property.Value }
        }
    }
    return $public
}

function Get-PublicProcessIdentity($Identity) {
    return [ordered]@{
        processId = $Identity.processId
        creationDate = $Identity.creationDate
        creationUtcTicks = $Identity.creationUtcTicks
        executablePath = $Identity.executablePath
    }
}

function Resolve-Executable([string[]] $Names) {
    foreach ($name in $Names) {
        $candidate = Get-Command $name -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty Source -First 1
        if ($candidate) { return $candidate }
    }
    return $null
}

function Get-NodeRuntime {
    $node = Resolve-Executable @('node.exe', 'node')
    $npx = Resolve-Executable @('npx.cmd', 'npx')
    $npm = Resolve-Executable @('npm.cmd', 'npm')
    if (-not $node -or -not $npx -or -not $npm) {
        throw 'Node.js, npm, and npx are required.'
    }
    $versionText = (& $node --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $versionText -notmatch '^v(?<major>\d+)\.') {
        throw "Unable to determine Node.js version from '$versionText'."
    }
    if ([int]$Matches.major -lt 20) {
        throw "Node.js 20 or newer is required; found $versionText."
    }
    return @{ node = $node; npm = $npm; npx = $npx; version = $versionText }
}

function Get-PlaywrightCoreRuntime($Runtime) {
    $runtimeRoot = Join-Path $StateRoot 'node-runtime'
    $packagePath = Join-Path $runtimeRoot 'node_modules\playwright-core\package.json'
    $valid = $false
    if (Test-Path -LiteralPath $packagePath -PathType Leaf) {
        try {
            $package = Get-Content -Raw -LiteralPath $packagePath | ConvertFrom-Json
            $valid = [string]$package.version -eq $playwrightCoreVersion
        } catch { $valid = $false }
    }
    if (-not $valid) {
        New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
        $installOutput = & $Runtime.npm install --prefix $runtimeRoot --no-save --no-package-lock `
            --ignore-scripts --no-audit --no-fund "playwright-core@$playwrightCoreVersion" 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to install the pinned Playwright collection runtime: $($installOutput -join [Environment]::NewLine)"
        }
        if (-not (Test-Path -LiteralPath $packagePath -PathType Leaf)) {
            throw 'The pinned Playwright collection runtime installation did not produce its package manifest.'
        }
        $package = Get-Content -Raw -LiteralPath $packagePath | ConvertFrom-Json
        if ([string]$package.version -ne $playwrightCoreVersion) {
            throw "The Playwright collection runtime version is $($package.version), expected $playwrightCoreVersion."
        }
    }
    return $runtimeRoot
}

function Get-ChromeExecutable {
    foreach ($candidate in @(
        'C:\Program Files\Google\Chrome\Application\chrome.exe',
        'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    throw 'Google Chrome is required for the dedicated browser profile.'
}

function Get-DedicatedChromeCdpEndpoint {
    $activePortPath = Join-Path $profileRoot 'DevToolsActivePort'
    if (-not (Test-Path -LiteralPath $activePortPath -PathType Leaf)) { return $null }
    $activePort = @(Get-Content -LiteralPath $activePortPath)
    if ($activePort.Count -lt 2 -or $activePort[0] -notmatch '^\d{1,5}$' -or
        [int]$activePort[0] -lt 1 -or [int]$activePort[0] -gt 65535 -or
        $activePort[1] -notmatch '^/devtools/browser/[A-Za-z0-9-]+$') { return $null }
    $chromePort = [int]$activePort[0]
    $listener = Get-NetTCPConnection -State Listen -LocalPort $chromePort -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') } |
        Select-Object -First 1
    if (-not $listener) { return $null }
    return @{
        endpoint = "ws://127.0.0.1:${chromePort}$($activePort[1])"
        port = $chromePort
        processId = [int]$listener.OwningProcess
    }
}

function Get-ChromeCdpEndpoint {
    $activePortPath = Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data\DevToolsActivePort'
    if (-not (Test-Path -LiteralPath $activePortPath -PathType Leaf)) {
        throw "Chrome remote debugging is not ready. Enable it at chrome://inspect/#remote-debugging."
    }
    $activePort = @(Get-Content -LiteralPath $activePortPath)
    if ($activePort.Count -lt 2 -or $activePort[0] -notmatch '^\d{1,5}$' -or
        [int]$activePort[0] -lt 1 -or [int]$activePort[0] -gt 65535 -or
        $activePort[1] -notmatch '^/devtools/browser/[A-Za-z0-9-]+$') {
        throw "Chrome's DevToolsActivePort file is malformed."
    }
    $chromePort = [int]$activePort[0]
    $chromeListener = Get-NetTCPConnection -State Listen -LocalPort $chromePort -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') } |
        Select-Object -First 1
    if (-not $chromeListener) {
        throw "Chrome's recorded remote-debugging port $chromePort is not listening on loopback."
    }
    return "ws://127.0.0.1:${chromePort}$($activePort[1])"
}

function Get-ListenerProcess([int] $ListenerPort) {
    return Get-NetTCPConnection -State Listen -LocalPort $ListenerPort -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @($bindHost, '0.0.0.0', '::', '::1') } |
        Select-Object -First 1
}

function Get-FreeLoopbackPort {
    $temporaryListener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try {
        $temporaryListener.Start()
        return ([Net.IPEndPoint]$temporaryListener.LocalEndpoint).Port
    } finally {
        $temporaryListener.Stop()
    }
}

function Invoke-BrokerShutdown([int] $BrokerPort, [string] $ShutdownToken) {
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [Net.Http.HttpClient]::new($handler)
    try {
        $client.Timeout = [TimeSpan]::FromSeconds(5)
        $request = [Net.Http.HttpRequestMessage]::new(
            [Net.Http.HttpMethod]::Post,
            "http://${bindHost}:$BrokerPort/shutdown"
        )
        $request.Headers.Add('X-Bridge-Shutdown', $ShutdownToken)
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "Browser broker rejected graceful shutdown with HTTP $([int]$response.StatusCode)."
        }
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-ProcessIdentity([int] $ProcessId) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    $creationDate = if ($process.CreationDate -is [DateTime]) {
        $process.CreationDate.ToUniversalTime()
    } else {
        [Management.ManagementDateTimeConverter]::ToDateTime([string]$process.CreationDate).ToUniversalTime()
    }
    return @{
        processId = [int]$process.ProcessId
        creationDate = $creationDate.ToString('o')
        creationUtcTicks = $creationDate.Ticks
        executablePath = $process.ExecutablePath
        commandLine = $process.CommandLine
    }
}

function Test-PlaywrightListenerIdentity($Identity, [int] $ListenerPort) {
    if (-not $Identity -or -not $Identity.commandLine) { return $false }
    $portPattern = "(?:--port(?:=|\s+))$ListenerPort(?:\s|$)"
    return (
        $Identity.commandLine -match 'playwright[\\/]mcp|playwright-mcp|@playwright/mcp' -and
        $Identity.commandLine -match $portPattern
    )
}

function Test-BrokerListenerIdentity($Identity, [int] $ListenerPort) {
    if (-not $Identity -or -not $Identity.commandLine) { return $false }
    $portPattern = "(?:--listen-port(?:=|\s+))$ListenerPort(?:\s|$)"
    return (
        $Identity.commandLine -match 'playwright_mcp_broker\.mjs' -and
        $Identity.commandLine -match $portPattern
    )
}

function Test-DedicatedChromeIdentity($Identity, [int] $ListenerPort) {
    if (-not $Identity -or -not $Identity.commandLine) { return $false }
    $profilePattern = [regex]::Escape([IO.Path]::GetFullPath($profileRoot))
    return (
        $Identity.executablePath -and
        [IO.Path]::GetFileName([string]$Identity.executablePath) -eq 'chrome.exe' -and
        $Identity.commandLine -match '--remote-debugging-port(?:=|\s+)(?:0|[0-9]+)' -and
        $Identity.commandLine -match $profilePattern
    )
}

function Read-Metadata {
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) { return $null }
    return Get-Content -Raw -LiteralPath $metadataPath | ConvertFrom-Json
}

function Write-Metadata($Metadata) {
    New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
    $temporaryMetadata = "$metadataPath.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $Metadata | ConvertTo-Json -Depth 4 |
            Set-Content -LiteralPath $temporaryMetadata -Encoding utf8NoBOM
        Move-Item -LiteralPath $temporaryMetadata -Destination $metadataPath -Force
    } finally {
        Remove-Item -LiteralPath $temporaryMetadata -Force -ErrorAction SilentlyContinue
    }
}

function Assert-ProcessIdentity($Metadata, [string] $Prefix, [scriptblock] $Matcher, [bool] $RequireListener = $true) {
    $processId = [int]$Metadata."${Prefix}ProcessId"
    $port = [int]$Metadata."${Prefix}Port"
    if ($RequireListener) {
        $listener = Get-ListenerProcess -ListenerPort $port
        if (-not $listener -or [int]$listener.OwningProcess -ne $processId) {
            throw "The recorded $Prefix process no longer owns port $port."
        }
    }
    $identity = Get-ProcessIdentity -ProcessId $processId
    if (-not $identity) { throw "The recorded $Prefix process no longer exists." }
    if ([long]$identity.creationUtcTicks -ne [long]$Metadata."${Prefix}CreationUtcTicks") {
        throw "The recorded $Prefix process ID has been reused; refusing to control it."
    }
    if (-not (& $Matcher $identity $port)) {
        throw "The recorded $Prefix process identity does not match its expected command."
    }
    return $identity
}

function Assert-OwnedService($Metadata) {
    $broker = Assert-ProcessIdentity $Metadata 'broker' ${function:Test-BrokerListenerIdentity}
    $playwright = Assert-ProcessIdentity $Metadata 'playwright' ${function:Test-PlaywrightListenerIdentity}
    $result = @{ broker = $broker; playwright = $playwright }
    if ($Metadata.PSObject.Properties.Name -contains 'chromeProcessId') {
        $result.chrome = Assert-ProcessIdentity $Metadata 'chrome' ${function:Test-DedicatedChromeIdentity}
    }
    return $result
}

function Stop-RecordedProcessIfOwned($Metadata, [string] $Prefix, [scriptblock] $Matcher, [bool] $RequireListener = $true) {
    $processIdProperty = "${Prefix}ProcessId"
    if (-not ($Metadata.PSObject.Properties.Name -contains $processIdProperty)) { return $false }
    $processId = [int]$Metadata.$processIdProperty
    if (-not (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { return $false }
    # Assert immediately before the destructive operation. If the listener or
    # creation identity changed, refusing is safer than killing a reused PID.
    $null = Assert-ProcessIdentity $Metadata $Prefix $Matcher $RequireListener
    Stop-Process -Id $processId -Force
    return $true
}

function Test-RecordedProcessAlive($Metadata, [string] $Prefix) {
    $property = "${Prefix}ProcessId"
    if (-not ($Metadata.PSObject.Properties.Name -contains $property)) { return $false }
    $identity = Get-ProcessIdentity -ProcessId ([int]$Metadata.$property)
    if (-not $identity) { return $false }
    return [long]$identity.creationUtcTicks -eq [long]$Metadata."${Prefix}CreationUtcTicks"
}

function Remove-MetadataIfOwned([string] $AttemptId) {
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) { return $true }
    $current = Read-Metadata
    if ($current.startAttemptId -ne $AttemptId) { return $false }
    Remove-Item -LiteralPath $metadataPath -Force
    return $true
}

function Remove-StaleManagedState($Metadata) {
    $failures = [Collections.Generic.List[string]]::new()
    $requireListeners = $Metadata.state -eq 'RUNNING'
    try { $null = Stop-RecordedProcessIfOwned $Metadata 'broker' ${function:Test-BrokerListenerIdentity} $requireListeners }
    catch { $failures.Add($_.Exception.Message) }
    try { $null = Stop-RecordedProcessIfOwned $Metadata 'playwright' ${function:Test-PlaywrightListenerIdentity} $requireListeners }
    catch { $failures.Add($_.Exception.Message) }
    try { $null = Stop-RecordedProcessIfOwned $Metadata 'chrome' ${function:Test-DedicatedChromeIdentity} $requireListeners }
    catch { $failures.Add($_.Exception.Message) }
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Min($TimeoutSeconds, 5))
    while ([DateTime]::UtcNow -lt $deadline -and
        ((Test-RecordedProcessAlive $Metadata 'broker') -or (Test-RecordedProcessAlive $Metadata 'playwright') -or
         (Test-RecordedProcessAlive $Metadata 'chrome'))) {
        Start-Sleep -Milliseconds 200
    }
    foreach ($prefix in @('broker', 'playwright', 'chrome')) {
        if (Test-RecordedProcessAlive $Metadata $prefix) { $failures.Add("The recorded $prefix process survived recovery cleanup.") }
    }
    if ($failures.Count) {
        throw "Stale bridge metadata has a live component that cannot be proven safe to stop: $($failures -join '; ')"
    }
    if (-not (Remove-MetadataIfOwned ([string]$Metadata.startAttemptId))) {
        throw 'Stale bridge metadata ownership changed during recovery; the newer record was preserved.'
    }
}

function Invoke-Probe([switch] $RequireBrowser) {
    $runtime = Get-NodeRuntime
    $probeArguments = @(
        $probeScript,
        '--endpoint', $endpoint,
        '--timeout-ms', ([string]($TimeoutSeconds * 1000))
    )
    if ($RequireBrowser) { $probeArguments += '--browser-check' }
    $output = & $runtime.node @probeArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Playwright MCP probe failed: $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine | ConvertFrom-Json)
}

function Invoke-ChatGptBootstrap([switch] $RetainAuthenticationTab) {
    $runtime = Get-NodeRuntime
    $bootstrapArguments = @(
        $bootstrapScript,
        '--endpoint', $endpoint,
        '--timeout-ms', ([string]($TimeoutSeconds * 1000))
    )
    if ($RetainAuthenticationTab) {
        $bootstrapArguments += @(
            '--keep-open',
            '--session-state', (Join-Path $StateRoot 'authentication-session.json')
        )
    }
    $output = & $runtime.node @bootstrapArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "ChatGPT browser bootstrap failed: $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine | ConvertFrom-Json)
}

function Invoke-ChatGptAttachmentCollection {
    if (-not $ManifestPath) { throw 'collect-attachment requires ManifestPath.' }
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Collection manifest does not exist: $ManifestPath"
    }
    $runtime = Get-NodeRuntime
    $playwrightRuntimeRoot = Get-PlaywrightCoreRuntime $runtime
    $dedicatedCdp = Get-DedicatedChromeCdpEndpoint
    if (-not $dedicatedCdp) { throw 'The managed dedicated Chrome CDP endpoint is unavailable.' }
    $outputRoot = Join-Path $StateRoot 'output'
    $collectorArguments = @(
        $directCollectorScript,
        '--cdp-endpoint', ([string]$dedicatedCdp.endpoint),
        '--playwright-root', $playwrightRuntimeRoot,
        '--timeout-ms', ([string]($TimeoutSeconds * 1000)),
        '--output-root', $outputRoot,
        '--manifest', ([IO.Path]::GetFullPath($ManifestPath))
    )
    $output = & $runtime.node @collectorArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "ChatGPT attachment collection failed: $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine | ConvertFrom-Json)
}

function Invoke-ChatGptDelivery {
    if (-not $ManifestPath) { throw 'deliver-message requires ManifestPath.' }
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Delivery manifest does not exist: $ManifestPath"
    }
    $runtime = Get-NodeRuntime
    $deliveryArguments = @(
        $deliveryScript,
        '--endpoint', $endpoint,
        '--timeout-ms', ([string]($TimeoutSeconds * 1000)),
        '--manifest', ([IO.Path]::GetFullPath($ManifestPath))
    )
    $output = & $runtime.node @deliveryArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "ChatGPT delivery failed: $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine | ConvertFrom-Json)
}

$playwrightLauncher = $null
$brokerLauncher = $null
$chromeLauncher = $null
$startedPlaywrightIdentity = $null
$startedBrokerIdentity = $null
$startedChromeIdentity = $null
$rawPort = $null
$chromePort = $null

try {
    if ($Command -in @('start', 'stop')) {
        $mutexName = 'Local\PostOfficeNext-PlaywrightBridge-' + [Convert]::ToHexString(
            [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes(
                [IO.Path]::GetFullPath($StateRoot).ToLowerInvariant()
            ))
        )
        $stateMutex = [Threading.Mutex]::new($false, $mutexName)
        $stateMutexOwned = $stateMutex.WaitOne(0)
        if (-not $stateMutexOwned) { throw 'Another bridge lifecycle operation is already active for this state root.' }
    }
    if ($Command -eq 'preflight') {
        $runtime = Get-NodeRuntime
        $cdpReady = $null
        $cdpDiagnostic = $null
        if ($BrowserMode -eq 'ExistingChrome') {
            try {
                $null = Get-ChromeCdpEndpoint
                $cdpReady = $true
            } catch {
                $cdpReady = $false
                $cdpDiagnostic = $_.Exception.Message
            }
        }
        Write-Result @{
            ok = $true
            command = $Command
            node = $runtime.node
            nodeVersion = $runtime.version
            npx = $runtime.npx
            npm = $runtime.npm
            package = "@playwright/mcp@$packageVersion"
            endpoint = $endpoint
            browserMode = $BrowserMode
            remoteDebuggingRequired = ($BrowserMode -eq 'ExistingChrome')
            persistentProfile = if ($BrowserMode -eq 'DedicatedChrome') { $profileRoot } else { $null }
            chromeCdpReady = $cdpReady
            chromeCdpDiagnostic = $cdpDiagnostic
        }
        exit 0
    }

    if ($Command -eq 'start') {
        Assert-SafeStateRoot
        $runtime = Get-NodeRuntime
        if (Test-Path -LiteralPath $metadataPath -PathType Leaf) {
            $existing = Read-Metadata
            if ($existing.state -eq 'RUNNING') {
              try {
                $null = Assert-OwnedService $existing
                throw "A managed bridge is already running on port $($existing.port)."
              } catch {
                if ($_.Exception.Message -like 'A managed bridge is already running*') { throw }
              }
            }
            Remove-StaleManagedState $existing
        }
        if (Get-ListenerProcess -ListenerPort $Port) {
            throw "Port $Port is already in use; refusing to replace or share the listener."
        }
        do { $rawPort = Get-FreeLoopbackPort } while ($rawPort -eq $Port)

        New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
        $outputRoot = Join-Path $StateRoot 'output'
        New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
        $stdoutPath = Join-Path $StateRoot 'bridge.stdout.log'
        $stderrPath = Join-Path $StateRoot 'bridge.stderr.log'
        $brokerStdoutPath = Join-Path $StateRoot 'broker.stdout.log'
        $brokerStderrPath = Join-Path $StateRoot 'broker.stderr.log'
        $arguments = @(
            '--yes', "@playwright/mcp@$packageVersion",
            '--port', [string]$rawPort,
            '--host', $bindHost,
            '--allowed-hosts', "${bindHost}:$rawPort,localhost:$rawPort",
            '--image-responses', 'omit',
            '--codegen', 'none',
            '--output-dir', $outputRoot,
            '--output-max-size', '268435456'
        )
        if ($BrowserMode -eq 'ExistingChrome') {
            $chromeCdpEndpoint = Get-ChromeCdpEndpoint
            $arguments += @('--cdp-endpoint', $chromeCdpEndpoint)
        } elseif ($BrowserMode -eq 'DedicatedChrome') {
            New-Item -ItemType Directory -Path $profileRoot -Force | Out-Null
            $dedicatedProcesses = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
                Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($profileRoot) })
            if ($dedicatedProcesses.Count) {
                throw 'The dedicated Chrome profile is already owned by an unmanaged process.'
            }
            $activePortPath = Join-Path $profileRoot 'DevToolsActivePort'
            Remove-Item -LiteralPath $activePortPath -Force -ErrorAction SilentlyContinue
            $chromeExecutable = Get-ChromeExecutable
            $chromeArguments = @(
                '--remote-debugging-port=0',
                "--user-data-dir=$profileRoot",
                '--no-first-run',
                'about:blank'
            ) | ForEach-Object { ConvertTo-NativeArgument ([string]$_) }
            $chromeLauncher = Start-Process -FilePath $chromeExecutable `
                -ArgumentList $chromeArguments -PassThru
            $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
            $dedicatedCdp = $null
            while ([DateTime]::UtcNow -lt $deadline) {
                if ($chromeLauncher.HasExited) {
                    throw "Dedicated Chrome exited during startup with code $($chromeLauncher.ExitCode)."
                }
                $dedicatedCdp = Get-DedicatedChromeCdpEndpoint
                if ($dedicatedCdp) { break }
                Start-Sleep -Milliseconds 250
            }
            if (-not $dedicatedCdp) {
                throw 'Dedicated Chrome did not expose its loopback CDP endpoint in time.'
            }
            $chromePort = [int]$dedicatedCdp.port
            $chromeIdentity = Get-ProcessIdentity -ProcessId ([int]$dedicatedCdp.processId)
            if (-not $chromeIdentity -or
                -not (Test-DedicatedChromeIdentity $chromeIdentity $chromePort)) {
                throw 'The dedicated Chrome listener identity could not be verified.'
            }
            $startedChromeIdentity = $chromeIdentity
            $arguments += @(
                '--cdp-endpoint', [string]$dedicatedCdp.endpoint,
                '--shared-browser-context'
            )
        } else {
            $arguments += @('--browser', 'chrome', '--isolated')
        }
        if ($Headless) { $arguments += '--headless' }

        $playwrightArguments = @($arguments | ForEach-Object { ConvertTo-NativeArgument ([string]$_) })
        $playwrightLauncher = Start-Process -FilePath $runtime.npx -ArgumentList $playwrightArguments `
            -WorkingDirectory $StateRoot -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
        $playwrightListener = $null
        while ([DateTime]::UtcNow -lt $deadline) {
            if ($playwrightLauncher.HasExited) {
                $errorTail = if (Test-Path -LiteralPath $stderrPath) {
                    (Get-Content -Tail 30 -LiteralPath $stderrPath) -join [Environment]::NewLine
                } else { '' }
                throw "Playwright MCP exited during startup with code $($playwrightLauncher.ExitCode). $errorTail"
            }
            $playwrightListener = Get-ListenerProcess -ListenerPort $rawPort
            if ($playwrightListener) { break }
            Start-Sleep -Milliseconds 250
        }
        if (-not $playwrightListener) {
            Stop-Process -Id $playwrightLauncher.Id -Force -ErrorAction SilentlyContinue
            throw "Playwright MCP did not bind its internal loopback port within $TimeoutSeconds seconds."
        }

        $playwrightIdentity = Get-ProcessIdentity -ProcessId ([int]$playwrightListener.OwningProcess)
        if (-not $playwrightIdentity) { throw 'Unable to record the Playwright MCP listener identity.' }
        if (-not (Test-PlaywrightListenerIdentity $playwrightIdentity $rawPort)) {
            throw 'The new listener process did not match the expected Playwright MCP command.'
        }
        $startedPlaywrightIdentity = $playwrightIdentity

        $partialMetadata = [ordered]@{
            schemaVersion = 4
            state = 'STARTING'
            startAttemptId = $startAttemptId
            package = "@playwright/mcp@$packageVersion"
            endpoint = $endpoint
            host = $bindHost
            port = $Port
            internalEndpoint = "http://${bindHost}:$rawPort/mcp"
            playwrightProcessId = $playwrightIdentity.processId
            playwrightPort = $rawPort
            playwrightCreationDate = $playwrightIdentity.creationDate
            playwrightCreationUtcTicks = $playwrightIdentity.creationUtcTicks
        }
        if ($startedChromeIdentity) {
            $partialMetadata.chromeProcessId = $startedChromeIdentity.processId
            $partialMetadata.chromePort = $chromePort
            $partialMetadata.chromeCreationDate = $startedChromeIdentity.creationDate
            $partialMetadata.chromeCreationUtcTicks = $startedChromeIdentity.creationUtcTicks
            $partialMetadata.chromeExecutablePath = $startedChromeIdentity.executablePath
        }
        Write-Metadata $partialMetadata
        $metadataOwnedByThisStart = $true

        $shutdownToken = [Convert]::ToHexString(
            [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
        ).ToLowerInvariant()
        $brokerArguments = @(
            $brokerScript,
            '--listen-port', [string]$Port,
            '--upstream', "http://${bindHost}:$rawPort/mcp",
            '--timeout-ms', '120000'
        )
        $encodedBrokerArguments = @($brokerArguments | ForEach-Object { ConvertTo-NativeArgument ([string]$_) })
        $brokerLauncher = Start-Process -FilePath $runtime.node -ArgumentList $encodedBrokerArguments `
            -WorkingDirectory $StateRoot -RedirectStandardOutput $brokerStdoutPath `
            -RedirectStandardError $brokerStderrPath -WindowStyle Hidden -PassThru `
            -Environment @{ PON_BROWSER_BRIDGE_SHUTDOWN_TOKEN = $shutdownToken }
        $launchedBrokerIdentity = Get-ProcessIdentity -ProcessId $brokerLauncher.Id
        if (-not $launchedBrokerIdentity -or -not (Test-BrokerListenerIdentity $launchedBrokerIdentity $Port)) {
            throw 'Unable to record the launched browser broker identity.'
        }
        $startedBrokerIdentity = $launchedBrokerIdentity
        $partialMetadata.browserMode = $BrowserMode
        $partialMetadata.persistentProfile = if ($BrowserMode -eq 'DedicatedChrome') { $profileRoot } else { $null }
        $partialMetadata.headless = [bool]$Headless
        $partialMetadata.playwrightLauncherProcessId = $playwrightLauncher.Id
        $partialMetadata.brokerLauncherProcessId = $brokerLauncher.Id
        $partialMetadata.brokerProcessId = $launchedBrokerIdentity.processId
        $partialMetadata.brokerPort = $Port
        $partialMetadata.brokerCreationDate = $launchedBrokerIdentity.creationDate
        $partialMetadata.brokerCreationUtcTicks = $launchedBrokerIdentity.creationUtcTicks
        $partialMetadata.brokerShutdownToken = $shutdownToken
        $partialMetadata.stdoutPath = $stdoutPath
        $partialMetadata.stderrPath = $stderrPath
        $partialMetadata.brokerStdoutPath = $brokerStdoutPath
        $partialMetadata.brokerStderrPath = $brokerStderrPath
        Write-Metadata $partialMetadata
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
        $brokerListener = $null
        while ([DateTime]::UtcNow -lt $deadline) {
            if ($brokerLauncher.HasExited) {
                $errorTail = if (Test-Path -LiteralPath $brokerStderrPath) {
                    (Get-Content -Tail 30 -LiteralPath $brokerStderrPath) -join [Environment]::NewLine
                } else { '' }
                throw "Browser broker exited during startup with code $($brokerLauncher.ExitCode). $errorTail"
            }
            $brokerListener = Get-ListenerProcess -ListenerPort $Port
            if ($brokerListener) { break }
            Start-Sleep -Milliseconds 250
        }
        if (-not $brokerListener) {
            throw "Browser broker did not bind $endpoint within $TimeoutSeconds seconds."
        }
        $brokerIdentity = Get-ProcessIdentity -ProcessId ([int]$brokerListener.OwningProcess)
        if (-not $brokerIdentity -or -not (Test-BrokerListenerIdentity $brokerIdentity $Port)) {
            throw 'The new listener process did not match the expected browser broker command.'
        }
        $startedBrokerIdentity = $brokerIdentity
        $partialMetadata.brokerProcessId = $brokerIdentity.processId
        $partialMetadata.brokerCreationDate = $brokerIdentity.creationDate
        $partialMetadata.brokerCreationUtcTicks = $brokerIdentity.creationUtcTicks
        Write-Metadata $partialMetadata
        $metadata = [ordered]@{
            schemaVersion = 4
            state = 'RUNNING'
            startAttemptId = $startAttemptId
            package = "@playwright/mcp@$packageVersion"
            endpoint = $endpoint
            host = $bindHost
            port = $Port
            internalEndpoint = "http://${bindHost}:$rawPort/mcp"
            browserMode = $BrowserMode
            persistentProfile = if ($BrowserMode -eq 'DedicatedChrome') { $profileRoot } else { $null }
            headless = [bool]$Headless
            playwrightLauncherProcessId = $playwrightLauncher.Id
            playwrightProcessId = $playwrightIdentity.processId
            playwrightPort = $rawPort
            playwrightCreationDate = $playwrightIdentity.creationDate
            playwrightCreationUtcTicks = $playwrightIdentity.creationUtcTicks
            brokerLauncherProcessId = $brokerLauncher.Id
            brokerProcessId = $brokerIdentity.processId
            brokerPort = $Port
            brokerCreationDate = $brokerIdentity.creationDate
            brokerCreationUtcTicks = $brokerIdentity.creationUtcTicks
            brokerShutdownToken = $shutdownToken
            startedAt = [DateTime]::UtcNow.ToString('o')
            stdoutPath = $stdoutPath
            stderrPath = $stderrPath
            brokerStdoutPath = $brokerStdoutPath
            brokerStderrPath = $brokerStderrPath
        }
        if ($startedChromeIdentity) {
            $metadata.chromeProcessId = $startedChromeIdentity.processId
            $metadata.chromePort = $chromePort
            $metadata.chromeCreationDate = $startedChromeIdentity.creationDate
            $metadata.chromeCreationUtcTicks = $startedChromeIdentity.creationUtcTicks
            $metadata.chromeExecutablePath = $startedChromeIdentity.executablePath
        }
        Write-Metadata $metadata

        $probe = Invoke-Probe -RequireBrowser:$BrowserCheck
        Write-Result @{ ok = $true; command = $Command; bridge = (Get-PublicMetadata $metadata); probe = $probe }
        exit 0
    }

    if ($Command -eq 'probe') {
        $probe = Invoke-Probe -RequireBrowser:$BrowserCheck
        Write-Result @{ ok = $true; command = $Command; probe = $probe }
        exit 0
    }

    $metadata = Read-Metadata
    if (-not $metadata) {
        Write-Result @{ ok = $true; command = $Command; running = $false; endpoint = $endpoint }
        exit 0
    }

    if ([int]$metadata.port -ne $Port) {
        throw "The managed bridge is recorded on port $($metadata.port), not requested port $Port."
    }

    if ($Command -eq 'status') {
        $identities = Assert-OwnedService $metadata
        $probe = Invoke-Probe -RequireBrowser:$BrowserCheck
        $publicProcesses = @{
            broker = (Get-PublicProcessIdentity $identities.broker)
            playwright = (Get-PublicProcessIdentity $identities.playwright)
        }
        if ($identities.chrome) {
            $publicProcesses.chrome = Get-PublicProcessIdentity $identities.chrome
        }
        Write-Result @{
            ok = $true
            command = $Command
            running = $true
            bridge = (Get-PublicMetadata $metadata)
            processes = $publicProcesses
            probe = $probe
        }
        exit 0
    }

    if ($Command -eq 'bootstrap') {
        $identities = Assert-OwnedService $metadata
        if ($metadata.browserMode -ne 'DedicatedChrome') {
            throw "ChatGPT bootstrap requires DedicatedChrome; the running mode is $($metadata.browserMode)."
        }
        $bootstrap = Invoke-ChatGptBootstrap -RetainAuthenticationTab:(-not [bool]$metadata.headless)
        Write-Result @{
            ok = $true
            command = $Command
            bridge = (Get-PublicMetadata $metadata)
            processes = @{
                broker = (Get-PublicProcessIdentity $identities.broker)
                playwright = (Get-PublicProcessIdentity $identities.playwright)
                chrome = (Get-PublicProcessIdentity $identities.chrome)
            }
            bootstrap = $bootstrap
        }
        exit 0
    }

    if ($Command -eq 'collect-attachment') {
        $identities = Assert-OwnedService $metadata
        if ($metadata.browserMode -ne 'DedicatedChrome') {
            throw "ChatGPT attachment collection requires DedicatedChrome; the running mode is $($metadata.browserMode)."
        }
        $collection = Invoke-ChatGptAttachmentCollection
        Write-Result @{
            ok = $true
            command = $Command
            bridge = (Get-PublicMetadata $metadata)
            collection = $collection
        }
        exit 0
    }

    if ($Command -eq 'deliver-message') {
        $identities = Assert-OwnedService $metadata
        if ($metadata.browserMode -ne 'DedicatedChrome') {
            throw "ChatGPT delivery requires DedicatedChrome; the running mode is $($metadata.browserMode)."
        }
        $delivery = Invoke-ChatGptDelivery
        Write-Result @{
            ok = $true
            command = $Command
            bridge = (Get-PublicMetadata $metadata)
            delivery = $delivery
        }
        exit 0
    }

    if ($Command -eq 'stop') {
        $identities = Assert-OwnedService $metadata
        Invoke-BrokerShutdown -BrokerPort ([int]$metadata.brokerPort) `
            -ShutdownToken ([string]$metadata.brokerShutdownToken)
        $brokerDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min($TimeoutSeconds, 5))
        while ([DateTime]::UtcNow -lt $brokerDeadline -and
            (Get-Process -Id $identities.broker.processId -ErrorAction SilentlyContinue)) {
            Start-Sleep -Milliseconds 200
        }
        $null = Stop-RecordedProcessIfOwned $metadata 'playwright' ${function:Test-PlaywrightListenerIdentity}
        $null = Stop-RecordedProcessIfOwned $metadata 'chrome' ${function:Test-DedicatedChromeIdentity}
        $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Min($TimeoutSeconds, 5))
        while ([DateTime]::UtcNow -lt $deadline -and
            ((Get-Process -Id $identities.broker.processId -ErrorAction SilentlyContinue) -or
             (Get-Process -Id $identities.playwright.processId -ErrorAction SilentlyContinue) -or
             ($identities.chrome -and (Get-Process -Id $identities.chrome.processId -ErrorAction SilentlyContinue)))) {
            Start-Sleep -Milliseconds 200
        }
        if (Get-Process -Id $identities.broker.processId -ErrorAction SilentlyContinue) {
            $null = Stop-RecordedProcessIfOwned $metadata 'broker' ${function:Test-BrokerListenerIdentity}
            throw 'The browser broker did not stop gracefully; its identity was revalidated and it was forced closed.'
        }
        if (Get-Process -Id $identities.playwright.processId -ErrorAction SilentlyContinue) {
            $null = Stop-RecordedProcessIfOwned $metadata 'playwright' ${function:Test-PlaywrightListenerIdentity}
            throw 'Playwright MCP did not stop after its identity was revalidated.'
        }
        if ($identities.chrome -and (Get-Process -Id $identities.chrome.processId -ErrorAction SilentlyContinue)) {
            $null = Stop-RecordedProcessIfOwned $metadata 'chrome' ${function:Test-DedicatedChromeIdentity}
            throw 'Dedicated Chrome did not stop after its identity was revalidated.'
        }
        if (-not (Remove-MetadataIfOwned ([string]$metadata.startAttemptId))) {
            throw 'Bridge stopped, but its management record changed ownership and was preserved.'
        }
        Remove-Item -LiteralPath (Join-Path $StateRoot 'authentication-session.json') `
            -Force -ErrorAction SilentlyContinue
        Write-Result @{
            ok = $true
            command = $Command
            running = $false
            stoppedProcessIds = @($identities.broker.processId, $identities.playwright.processId,
                $(if ($identities.chrome) { $identities.chrome.processId })) | Where-Object { $_ }
            endpoint = $metadata.endpoint
        }
        exit 0
    }
} catch {
    $caughtException = $_.Exception
    $reportedCleanupFailures = @()
    $reportedSurvivors = @()
    if ($Command -eq 'start') {
        $startupError = $caughtException.Message
        $cleanupFailures = [Collections.Generic.List[string]]::new()
        $survivors = [Collections.Generic.List[string]]::new()
        $ownedMetadata = $null
        if ($metadataOwnedByThisStart -and (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
            $candidateMetadata = Read-Metadata
            if ($candidateMetadata.startAttemptId -eq $startAttemptId) { $ownedMetadata = $candidateMetadata }
        }
        if ($ownedMetadata) {
            try { $null = Stop-RecordedProcessIfOwned $ownedMetadata 'broker' ${function:Test-BrokerListenerIdentity} $false }
            catch { $cleanupFailures.Add("broker: $($_.Exception.Message)") }
            try { $null = Stop-RecordedProcessIfOwned $ownedMetadata 'playwright' ${function:Test-PlaywrightListenerIdentity} $false }
            catch { $cleanupFailures.Add("playwright: $($_.Exception.Message)") }
            try { $null = Stop-RecordedProcessIfOwned $ownedMetadata 'chrome' ${function:Test-DedicatedChromeIdentity} $false }
            catch { $cleanupFailures.Add("chrome: $($_.Exception.Message)") }
            $cleanupDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min($TimeoutSeconds, 5))
            while ([DateTime]::UtcNow -lt $cleanupDeadline -and
                ((Test-RecordedProcessAlive $ownedMetadata 'broker') -or
                 (Test-RecordedProcessAlive $ownedMetadata 'playwright') -or
                 (Test-RecordedProcessAlive $ownedMetadata 'chrome'))) {
                Start-Sleep -Milliseconds 200
            }
            foreach ($prefix in @('broker', 'playwright', 'chrome')) {
                if (Test-RecordedProcessAlive $ownedMetadata $prefix) { $survivors.Add($prefix) }
            }
            if ($survivors.Count -eq 0 -and $cleanupFailures.Count -eq 0) {
                if (-not (Remove-MetadataIfOwned $startAttemptId)) {
                    $cleanupFailures.Add('management record changed ownership before cleanup completion')
                }
            } else {
                $ownedMetadata.state = 'RECOVERY_REQUIRED'
                $ownedMetadata | Add-Member -NotePropertyName startupError -NotePropertyValue $startupError -Force
                $ownedMetadata | Add-Member -NotePropertyName cleanupFailures -NotePropertyValue @($cleanupFailures) -Force
                $ownedMetadata | Add-Member -NotePropertyName survivingComponents -NotePropertyValue @($survivors) -Force
                Write-Metadata $ownedMetadata
            }
        } elseif ($startedBrokerIdentity -or $startedPlaywrightIdentity -or $startedChromeIdentity) {
            # A metadata write itself may have failed after a process identity
            # was captured. Reconstruct the exact minimum recovery record from
            # memory, attempt bounded cleanup, and persist any survivor rather
            # than abandoning an unowned component.
            $recoveryMetadata = [ordered]@{
                schemaVersion = 4
                state = 'RECOVERY_REQUIRED'
                startAttemptId = $startAttemptId
                endpoint = $endpoint
                host = $bindHost
                port = $Port
                startupError = $startupError
            }
            if ($startedPlaywrightIdentity) {
                $recoveryMetadata.playwrightProcessId = $startedPlaywrightIdentity.processId
                $recoveryMetadata.playwrightPort = $rawPort
                $recoveryMetadata.playwrightCreationDate = $startedPlaywrightIdentity.creationDate
                $recoveryMetadata.playwrightCreationUtcTicks = $startedPlaywrightIdentity.creationUtcTicks
            }
            if ($startedBrokerIdentity) {
                $recoveryMetadata.brokerProcessId = $startedBrokerIdentity.processId
                $recoveryMetadata.brokerPort = $Port
                $recoveryMetadata.brokerCreationDate = $startedBrokerIdentity.creationDate
                $recoveryMetadata.brokerCreationUtcTicks = $startedBrokerIdentity.creationUtcTicks
            }
            if ($startedChromeIdentity) {
                $recoveryMetadata.chromeProcessId = $startedChromeIdentity.processId
                $recoveryMetadata.chromePort = $chromePort
                $recoveryMetadata.chromeCreationDate = $startedChromeIdentity.creationDate
                $recoveryMetadata.chromeCreationUtcTicks = $startedChromeIdentity.creationUtcTicks
                $recoveryMetadata.chromeExecutablePath = $startedChromeIdentity.executablePath
            }
            $ownedMetadata = [pscustomobject]$recoveryMetadata
            try { $null = Stop-RecordedProcessIfOwned $ownedMetadata 'broker' ${function:Test-BrokerListenerIdentity} $false }
            catch { $cleanupFailures.Add("broker: $($_.Exception.Message)") }
            try { $null = Stop-RecordedProcessIfOwned $ownedMetadata 'playwright' ${function:Test-PlaywrightListenerIdentity} $false }
            catch { $cleanupFailures.Add("playwright: $($_.Exception.Message)") }
            try { $null = Stop-RecordedProcessIfOwned $ownedMetadata 'chrome' ${function:Test-DedicatedChromeIdentity} $false }
            catch { $cleanupFailures.Add("chrome: $($_.Exception.Message)") }
            $cleanupDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min($TimeoutSeconds, 5))
            while ([DateTime]::UtcNow -lt $cleanupDeadline -and
                ((Test-RecordedProcessAlive $ownedMetadata 'broker') -or
                 (Test-RecordedProcessAlive $ownedMetadata 'playwright') -or
                 (Test-RecordedProcessAlive $ownedMetadata 'chrome'))) {
                Start-Sleep -Milliseconds 200
            }
            foreach ($prefix in @('broker', 'playwright', 'chrome')) {
                if (Test-RecordedProcessAlive $ownedMetadata $prefix) { $survivors.Add($prefix) }
            }
            if ($survivors.Count -gt 0 -or $cleanupFailures.Count -gt 0) {
                $ownedMetadata | Add-Member -NotePropertyName cleanupFailures -NotePropertyValue @($cleanupFailures) -Force
                $ownedMetadata | Add-Member -NotePropertyName survivingComponents -NotePropertyValue @($survivors) -Force
                try {
                    Write-Metadata $ownedMetadata
                    $metadataOwnedByThisStart = $true
                } catch {
                    $cleanupFailures.Add("recovery record: $($_.Exception.Message)")
                }
            }
        }
        $reportedCleanupFailures = @($cleanupFailures)
        $reportedSurvivors = @($survivors)
        $caughtException.Data['cleanupFailures'] = $reportedCleanupFailures
        $caughtException.Data['survivingComponents'] = $reportedSurvivors
    }
    [Console]::Error.WriteLine((@{
        ok = $false
        command = $Command
        error = $caughtException.Message
        endpoint = $endpoint
        cleanupFailures = $reportedCleanupFailures
        survivingComponents = $reportedSurvivors
    } | ConvertTo-Json -Depth 4 -Compress))
    exit 1
} finally {
    if ($stateMutexOwned) { $stateMutex.ReleaseMutex() }
    if ($stateMutex) { $stateMutex.Dispose() }
}
