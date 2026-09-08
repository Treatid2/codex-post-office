# SPDX-License-Identifier: MPL-2.0

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CommandArgs
)

$ErrorActionPreference = 'Stop'
$python = $env:CODEX_PYTHON
if (-not $python) {
    throw 'CODEX_PYTHON must name a deterministic Python 3 entry point.'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "The deterministic Codex Python entry point is unavailable: $python"
}
& $python (Join-Path $PSScriptRoot 'post_office_next.py') @CommandArgs
exit $LASTEXITCODE
