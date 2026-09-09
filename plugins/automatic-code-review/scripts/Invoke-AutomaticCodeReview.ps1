# SPDX-License-Identifier: MPL-2.0

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

$ErrorActionPreference = 'Stop'
$SchemaVersion = '1.0'
$Configuration = Join-Path $env:LOCALAPPDATA 'Treatid2\CodexPostOffice\automatic-code-review\runtime-lock.json'
$Client = Join-Path $PSScriptRoot 'review_client.py'
$ExpectedClientSha256 = 'd3f2ef45e3e0405052166061fd0f4c4bd0a71841a28ee8a103436ef8662b9bfd'

function Write-ReviewBootstrapFailure {
    param([string] $Code, [string] $Message)
    $payload = [ordered]@{
        schemaVersion = $SchemaVersion
        ok = $false
        diagnostic = [ordered]@{
            code = $Code
            message = $Message
            details = @{}
        }
    }
    [Console]::Error.WriteLine(($payload | ConvertTo-Json -Depth 5 -Compress))
    exit 2
}

function Assert-AttestedFile {
    param([string] $Path, [string] $ExpectedSha256, [string] $Label)
    if (-not [IO.Path]::IsPathFullyQualified($Path) -or
        -not (Test-Path -LiteralPath $Path -PathType Leaf) -or
        $ExpectedSha256 -notmatch '^[0-9a-f]{64}$') {
        Write-ReviewBootstrapFailure 'REVIEW_BOOTSTRAP_UNAVAILABLE' "$Label is unavailable."
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256) {
        Write-ReviewBootstrapFailure 'REVIEW_BOOTSTRAP_ATTESTATION_FAILED' "$Label failed attestation."
    }
}

try {
    if (-not $env:LOCALAPPDATA -or -not (Test-Path -LiteralPath $Configuration -PathType Leaf)) {
        Write-ReviewBootstrapFailure 'REVIEW_SERVICE_NOT_CONFIGURED' 'Run Configure-AutomaticCodeReview.ps1 from a trusted administrator task first.'
    }
    $configurationValue = Get-Content -Raw -LiteralPath $Configuration | ConvertFrom-Json -ErrorAction Stop
    if ($configurationValue.schemaVersion -ne '1.0') {
        Write-ReviewBootstrapFailure 'REVIEW_SERVICE_CONFIGURATION_INVALID' 'The automatic-review deployment lock has an unsupported schema.'
    }
    $Python = [string]$configurationValue.pythonExecutable
    $pythonName = [IO.Path]::GetFileName($Python)
    $pythonDigest = [string]$configurationValue.pythonFiles.$pythonName
    Assert-AttestedFile -Path $Python -ExpectedSha256 $pythonDigest -Label 'Registered Python runtime'
    Assert-AttestedFile -Path $Client -ExpectedSha256 $ExpectedClientSha256 -Label 'Automatic-review client'

    $clientBytes = [System.IO.File]::ReadAllBytes($Client)
    $encodedClient = [Convert]::ToBase64String($clientBytes)
    $runner = "import base64,sys;path=sys.argv[1];sys.argv=sys.argv[1:];scope={'__name__':'__main__','__file__':path};exec(compile(base64.b64decode(sys.stdin.read()),path,'exec'),scope,scope)"

    if ($Arguments -contains '--help' -or $Arguments -contains '-h') {
        $encodedClient | & $Python -I -c $runner $Client @Arguments
        exit $LASTEXITCODE
    }

    $output = ($encodedClient | & $Python -I -c $runner $Client @Arguments 2>&1 | Out-String).Trim()
    $exitCode = $LASTEXITCODE
    try {
        $null = $output | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        Write-ReviewBootstrapFailure 'REVIEW_BOOTSTRAP_PROTOCOL_ERROR' 'The automatic-review client did not return its versioned JSON protocol.'
    }
    if ($exitCode -eq 0) {
        [Console]::Out.WriteLine($output)
    }
    else {
        [Console]::Error.WriteLine($output)
    }
    exit $exitCode
}
catch {
    Write-ReviewBootstrapFailure 'REVIEW_BOOTSTRAP_FAILURE' 'The automatic-review wrapper failed before a verified operation result was available.'
}
