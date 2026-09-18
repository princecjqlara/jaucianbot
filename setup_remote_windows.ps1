param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$BaseUrl
)

$ErrorActionPreference = 'Stop'
$BaseUrl = $BaseUrl.TrimEnd('/')
if (-not $BaseUrl.StartsWith('https://')) { throw 'Use the production HTTPS URL for the Vercel deployment.' }
$secureKey = Read-Host 'Paste INSIGHTS_API_KEY from Vercel (input is hidden)' -AsSecureString
if ($secureKey.Length -eq 0) { throw 'No API key entered.' }
ConvertFrom-SecureString $secureKey | Set-Content -LiteralPath (Join-Path $PSScriptRoot '.insights-api-key.dpapi') -NoNewline
$BaseUrl | Set-Content -LiteralPath (Join-Path $PSScriptRoot '.insights-base-url') -NoNewline
& (Join-Path $PSScriptRoot 'remote_windows.ps1') status
if ($LASTEXITCODE -ne 0) { throw 'Remote archive verification failed.' }
