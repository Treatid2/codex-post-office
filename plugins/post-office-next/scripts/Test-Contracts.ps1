# SPDX-License-Identifier: MPL-2.0

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Join-Path (Split-Path $PSScriptRoot -Parent) 'contracts\v1'
$goodPass = 0
$badRejected = 0
$errors = @()

function Get-SchemaPath([System.IO.FileInfo]$Fixture) {
    if ($Fixture.BaseName.StartsWith('entity.')) {
        $name = $Fixture.BaseName.Substring(7)
        return Join-Path $root "entities\$name.schema.json"
    }
    $stem = $Fixture.BaseName.Substring(10)
    if ($stem.EndsWith('.request')) {
        $name = $stem.Substring(0, $stem.Length - 8)
        return Join-Path $root "operations\requests\$name.schema.json"
    }
    if ($stem -match '^(.*)\.result\.(success|failure)$') {
        return Join-Path $root "operations\results\$($Matches[1]).schema.json"
    }
    throw "Unrecognized contract fixture name: $($Fixture.Name)"
}

Get-ChildItem -LiteralPath (Join-Path $root 'fixtures\good') -File | ForEach-Object {
    $schema = Get-SchemaPath $_
    try {
        if (Test-Json -Json (Get-Content -LiteralPath $_.FullName -Raw) -SchemaFile $schema -ErrorAction Stop) {
            $script:goodPass++
        } else {
            $script:errors += $_.Name
        }
    } catch {
        $script:errors += $_.Name
    }
}

Get-ChildItem -LiteralPath (Join-Path $root 'fixtures\bad') -File | ForEach-Object {
    $schema = Get-SchemaPath $_
    if (-not (Test-Json -Json (Get-Content -LiteralPath $_.FullName -Raw) -SchemaFile $schema -ErrorAction SilentlyContinue)) {
        $script:badRejected++
    } else {
        $script:errors += $_.Name
    }
}

$bundleSchema = Join-Path $root 'entities\message-bundle.schema.json'
$bundleFixture = Get-Content -Raw -LiteralPath (Join-Path $root 'fixtures\good\entity.message-bundle.json')
$patternCases = @(
    @{ Label = 'draft07-traversal-pattern-accepted'; Apply = { param($item) $item.payloads[0].path = '../caller-secrets/token' } },
    @{ Label = 'draft07-filename-substring-accepted'; Apply = { param($item) $item.canonicalFilename = '../evil.json' } },
    @{ Label = 'draft07-digest-substring-accepted'; Apply = { param($item) $item.sha256 = 'junk' + ('a' * 64) + 'junk' } },
    @{ Label = 'draft07-id-substring-accepted'; Apply = { param($item) $item.id = '!PON-BUNDLE-001!' } }
)
foreach ($case in $patternCases) {
    $maliciousBundle = $bundleFixture | ConvertFrom-Json
    & $case.Apply $maliciousBundle
    if (Test-Json -Json ($maliciousBundle | ConvertTo-Json -Depth 20) -SchemaFile $bundleSchema -ErrorAction SilentlyContinue) {
        $errors += $case.Label
    }
}

$storageSchema = Join-Path $root 'entities\storage-copy.schema.json'
$maliciousTimestamp = Get-Content -Raw -LiteralPath (Join-Path $root 'fixtures\good\entity.storage-copy.json') | ConvertFrom-Json
$maliciousTimestamp.verifiedAt = 'x2026-09-04T12:00:00Zx'
if (Test-Json -Json ($maliciousTimestamp | ConvertTo-Json -Depth 20) -SchemaFile $storageSchema -ErrorAction SilentlyContinue) {
    $errors += 'draft07-timestamp-substring-accepted'
}

$result = [pscustomobject]@{
    ok = ($errors.Count -eq 0)
    goodPassed = $goodPass
    badRejected = $badRejected
    errors = $errors
}
$result | ConvertTo-Json -Depth 3
if (-not $result.ok) {
    exit 1
}
