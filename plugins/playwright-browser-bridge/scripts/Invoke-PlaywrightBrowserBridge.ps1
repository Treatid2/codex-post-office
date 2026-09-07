# SPDX-License-Identifier: MPL-2.0

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('preflight', 'start', 'status', 'probe', 'stop')]
    [string] $Command,

    [ValidateRange(1024, 65535)]
    [int] $Port = 8931,

    [ValidateSet('DedicatedChrome', 'ExistingChrome', 'ManagedChrome')]
    [string] $BrowserMode = 'DedicatedChrome',

    [switch] $Headless,
    [switch] $BrowserCheck,

    [ValidateRange(1, 120)]
    [int] $TimeoutSeconds = 30,

    [string] $StateRoot = (Join-Path $env:LOCALAPPDATA 'Treatid2\CodexPostOffice\playwright-browser-bridge')
)

$ErrorActionPreference = 'Stop'
$packageVersion = '0.0.80'
$bindHost = '127.0.0.1'
$endpoint = "http://${bindHost}:$Port/mcp"
$metadataPath = Join-Path $StateRoot 'bridge.json'
$probeScript = Join-Path $PSScriptRoot 'playwright_bridge_probe.mjs'
$brokerScript = Join-Path $PSScriptRoot 'playwright_mcp_broker.mjs'
$profileRoot = Join-Path $StateRoot 'chrome-profile'

function Write-Result([hashtable] $Value) {
    $Value | ConvertTo-Json -Depth 8 -Compress
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
    if (-not $node -or -not $npx) {
        throw 'Node.js and npx are required.'
    }
    $versionText = (& $node --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $versionText -notmatch '^v(?<major>\d+)\.') {
        throw "Unable to determine Node.js version from '$versionText'."
    }
    if ([int]$Matches.major -lt 20) {
        throw "Node.js 20 or newer is required; found $versionText."
    }
    return @{ node = $node; npx = $npx; version = $versionText }
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

function Read-Metadata {
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) { return $null }
    return Get-Content -Raw -LiteralPath $metadataPath | ConvertFrom-Json
}

function Assert-ProcessIdentity($Metadata, [string] $Prefix, [scriptblock] $Matcher) {
    $processId = [int]$Metadata."${Prefix}ProcessId"
    $port = [int]$Metadata."${Prefix}Port"
    $listener = Get-ListenerProcess -ListenerPort $port
    if (-not $listener -or [int]$listener.OwningProcess -ne $processId) {
        throw "The recorded $Prefix process no longer owns port $port."
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
    return @{ broker = $broker; playwright = $playwright }
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

$playwrightLauncher = $null
$brokerLauncher = $null
$startedPlaywrightIdentity = $null
$startedBrokerIdentity = $null
$rawPort = $null

try {
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
        $runtime = Get-NodeRuntime
        if (Test-Path -LiteralPath $metadataPath -PathType Leaf) {
            $existing = Read-Metadata
            try {
                $null = Assert-OwnedService $existing
                throw "A managed bridge is already running on port $($existing.port)."
            } catch {
                if ($_.Exception.Message -like 'A managed bridge is already running*') { throw }
                Remove-Item -LiteralPath $metadataPath -Force
            }
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
            $arguments += @('--browser', 'chrome', '--user-data-dir', $profileRoot)
        } else {
            $arguments += @('--browser', 'chrome', '--isolated')
        }
        if ($Headless) { $arguments += '--headless' }

        $playwrightLauncher = Start-Process -FilePath $runtime.npx -ArgumentList $arguments `
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

        $brokerArguments = @(
            $brokerScript,
            '--listen-port', [string]$Port,
            '--upstream', "http://${bindHost}:$rawPort/mcp",
            '--timeout-ms', '120000',
            '--shutdown-token', ([Convert]::ToHexString(
                [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
            ).ToLowerInvariant())
        )
        $shutdownToken = $brokerArguments[-1]
        $brokerLauncher = Start-Process -FilePath $runtime.node -ArgumentList $brokerArguments `
            -WorkingDirectory $StateRoot -RedirectStandardOutput $brokerStdoutPath `
            -RedirectStandardError $brokerStderrPath -WindowStyle Hidden -PassThru
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
        $metadata = [ordered]@{
            schemaVersion = 2
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
        $temporaryMetadata = "$metadataPath.tmp"
        $metadata | ConvertTo-Json -Depth 4 |
            Set-Content -LiteralPath $temporaryMetadata -Encoding utf8NoBOM
        Move-Item -LiteralPath $temporaryMetadata -Destination $metadataPath -Force

        $probe = Invoke-Probe -RequireBrowser:$BrowserCheck
        Write-Result @{ ok = $true; command = $Command; bridge = $metadata; probe = $probe }
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
        Write-Result @{
            ok = $true
            command = $Command
            running = $true
            bridge = $metadata
            processes = $identities
            probe = $probe
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
        if (Get-Process -Id $identities.playwright.processId -ErrorAction SilentlyContinue) {
            Stop-Process -Id $identities.playwright.processId -Force
        }
        $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Min($TimeoutSeconds, 5))
        while ([DateTime]::UtcNow -lt $deadline -and
            ((Get-Process -Id $identities.broker.processId -ErrorAction SilentlyContinue) -or
             (Get-Process -Id $identities.playwright.processId -ErrorAction SilentlyContinue))) {
            Start-Sleep -Milliseconds 200
        }
        if ((Get-Process -Id $identities.broker.processId -ErrorAction SilentlyContinue) -or
            (Get-Process -Id $identities.playwright.processId -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $identities.broker.processId -Force -ErrorAction SilentlyContinue
            Stop-Process -Id $identities.playwright.processId -Force -ErrorAction SilentlyContinue
            throw 'The browser bridge did not stop gracefully; its verified processes were forced closed.'
        }
        Remove-Item -LiteralPath $metadataPath -Force
        Write-Result @{
            ok = $true
            command = $Command
            running = $false
            stoppedProcessIds = @($identities.broker.processId, $identities.playwright.processId)
            endpoint = $metadata.endpoint
        }
        exit 0
    }
} catch {
    if ($Command -eq 'start') {
        if ($startedBrokerIdentity) {
            $currentListener = Get-ListenerProcess -ListenerPort $Port
            $currentIdentity = Get-ProcessIdentity -ProcessId $startedBrokerIdentity.processId
            if ($currentListener -and
                [int]$currentListener.OwningProcess -eq [int]$startedBrokerIdentity.processId -and
                [long]$currentIdentity.creationUtcTicks -eq [long]$startedBrokerIdentity.creationUtcTicks -and
                (Test-BrokerListenerIdentity $currentIdentity $Port)) {
                Stop-Process -Id $startedBrokerIdentity.processId -Force -ErrorAction SilentlyContinue
            }
        } elseif ($brokerLauncher -and -not $brokerLauncher.HasExited) {
            Stop-Process -Id $brokerLauncher.Id -Force -ErrorAction SilentlyContinue
        }
        if ($startedPlaywrightIdentity) {
            $currentListener = Get-ListenerProcess -ListenerPort $rawPort
            $currentIdentity = Get-ProcessIdentity -ProcessId $startedPlaywrightIdentity.processId
            if ($currentListener -and $currentIdentity -and
                [int]$currentListener.OwningProcess -eq [int]$startedPlaywrightIdentity.processId -and
                [long]$currentIdentity.creationUtcTicks -eq [long]$startedPlaywrightIdentity.creationUtcTicks -and
                (Test-PlaywrightListenerIdentity $currentIdentity $rawPort)) {
                Stop-Process -Id $startedPlaywrightIdentity.processId -Force -ErrorAction SilentlyContinue
            }
        } elseif ($playwrightLauncher -and -not $playwrightLauncher.HasExited) {
            Stop-Process -Id $playwrightLauncher.Id -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $metadataPath -PathType Leaf) {
            Remove-Item -LiteralPath $metadataPath -Force -ErrorAction SilentlyContinue
        }
    }
    [Console]::Error.WriteLine((@{
        ok = $false
        command = $Command
        error = $_.Exception.Message
        endpoint = $endpoint
    } | ConvertTo-Json -Depth 4 -Compress))
    exit 1
}
