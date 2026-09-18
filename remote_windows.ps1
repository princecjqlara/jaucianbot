param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$keyPath = Join-Path $PSScriptRoot '.insights-api-key.dpapi'
$urlPath = Join-Path $PSScriptRoot '.insights-base-url'
if (-not (Test-Path -LiteralPath $keyPath) -or -not (Test-Path -LiteralPath $urlPath)) {
    throw 'Remote access is not configured. Run setup_remote_windows.ps1 after deployment.'
}
$secureKey = Get-Content -LiteralPath $keyPath -Raw | ConvertTo-SecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $env:INSIGHTS_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $env:INSIGHTS_BASE_URL = Get-Content -LiteralPath $urlPath -Raw
    & py (Join-Path $PSScriptRoot 'remote_query.py') @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Remote query failed with exit code $LASTEXITCODE." }
}
finally {
    Remove-Item Env:INSIGHTS_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:INSIGHTS_BASE_URL -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
