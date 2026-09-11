# SPDX-License-Identifier: MPL-2.0

[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory = $true)]
    [string] $PythonExecutable,

    [Parameter(Mandatory = $true)]
    [string] $BackendEntrypoint,

    [Parameter(Mandatory = $true)]
    [string] $StateRoot
)

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

$ErrorActionPreference = 'Stop'
$configurationRoot = Join-Path $env:LOCALAPPDATA 'Treatid2\CodexPostOffice\automatic-code-review'
$configurationPath = Join-Path $configurationRoot 'runtime-lock.json'

function Resolve-RegularFile([string] $Value, [string] $Label) {
    $resolved = [IO.Path]::GetFullPath($Value)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "$Label does not exist: $resolved"
    }
    $item = Get-Item -LiteralPath $resolved -Force
    if ($item.LinkType) {
        throw "$Label must not be a symbolic link or junction: $resolved"
    }
    return $resolved
}

function Get-Digest([string] $Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

try {
    if (-not $env:LOCALAPPDATA) {
        throw 'LOCALAPPDATA is unavailable.'
    }
    $python = Resolve-RegularFile $PythonExecutable 'Python executable'
    $backend = Resolve-RegularFile $BackendEntrypoint 'Review backend'
    $backendDependency = Resolve-RegularFile (Join-Path (Split-Path $backend -Parent) 'codex_comms.py') 'Review backend dependency'
    $state = [IO.Path]::GetFullPath($StateRoot)
    $databaseCandidates = @(
        (Join-Path $state 'post-office-next.sqlite3'),
        (Join-Path $state 'hub.sqlite3')
    )
    $database = $databaseCandidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
    if (-not $database) {
        throw "Post Office database does not exist. Checked: $($databaseCandidates -join ', ')"
    }

    $pythonDirectory = Split-Path $python -Parent
    $runtimeFiles = [ordered]@{}
    $runtimeFiles[[IO.Path]::GetFileName($python)] = Get-Digest $python
    Get-ChildItem -LiteralPath $pythonDirectory -File |
        Where-Object { $_.Name -match '^(python(?:\d+)?|vcruntime\d+(?:_\d+)?)\.dll$' } |
        Sort-Object Name |
        ForEach-Object { $runtimeFiles[$_.Name] = Get-Digest $_.FullName }

    $backendFiles = [ordered]@{}
    $backendFiles[[IO.Path]::GetFileName($backend)] = Get-Digest $backend
    $backendFiles[[IO.Path]::GetFileName($backendDependency)] = Get-Digest $backendDependency

    $lock = [ordered]@{
        schemaVersion = '1.0'
        pythonExecutable = $python
        pythonFiles = $runtimeFiles
        backendEntrypoint = $backend
        backendFiles = $backendFiles
        stateRoot = $state
        stateDatabase = [IO.Path]::GetFileName($database)
        createdAt = [DateTime]::UtcNow.ToString('o')
    }

    if ($PSCmdlet.ShouldProcess($configurationPath, 'Write automatic-review deployment lock')) {
        New-Item -ItemType Directory -Path $configurationRoot -Force | Out-Null
        $temporary = Join-Path $configurationRoot ('.runtime-lock.' + [guid]::NewGuid().ToString('N') + '.tmp')
        try {
            $lock | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporary -Encoding utf8NoBOM
            Move-Item -LiteralPath $temporary -Destination $configurationPath -Force
        }
        finally {
            if (Test-Path -LiteralPath $temporary -PathType Leaf) {
                Remove-Item -LiteralPath $temporary -Force
            }
        }
    }

    @{
        ok = $true
        schemaVersion = '1.0'
        configurationPath = $configurationPath
        pythonFileCount = $runtimeFiles.Count
        backendFileCount = $backendFiles.Count
        stateDatabasePresent = $true
        stateDatabase = [IO.Path]::GetFileName($database)
    } | ConvertTo-Json -Depth 4 -Compress
}
catch {
    @{
        ok = $false
        schemaVersion = '1.0'
        diagnostic = @{
            code = 'REVIEW_CONFIGURATION_FAILED'
            message = $_.Exception.Message
        }
    } | ConvertTo-Json -Depth 4 -Compress | Write-Error
    exit 1
}
