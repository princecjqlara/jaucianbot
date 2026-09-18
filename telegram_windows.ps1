param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('verify', 'collect', 'webhook-info', 'set-webhook', 'delete-webhook')]
    [string]$Command,
    [Parameter(Position = 1)]
    [string]$Url
)

$ErrorActionPreference = 'Stop'
$tokenPath = Join-Path $PSScriptRoot '.telegram-token.dpapi'
if (-not (Test-Path -LiteralPath $tokenPath)) {
    throw 'Encrypted token file missing. Run setup_windows.ps1 first.'
}

$secureToken = Get-Content -LiteralPath $tokenPath -Raw | ConvertTo-SecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $env:TELEGRAM_BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ($Command -eq 'set-webhook') {
        if (-not $Url) { throw 'Provide the production URL ending in /api/webhook.' }
        $secureSecret = Read-Host 'Paste TELEGRAM_WEBHOOK_SECRET from Vercel (input is hidden)' -AsSecureString
        $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
        try {
            $env:TELEGRAM_WEBHOOK_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
            & py (Join-Path $PSScriptRoot 'manage_webhook.py') set $Url
        }
        finally {
            Remove-Item Env:TELEGRAM_WEBHOOK_SECRET -ErrorAction SilentlyContinue
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
        }
    }
    elseif ($Command -eq 'webhook-info') {
        & py (Join-Path $PSScriptRoot 'manage_webhook.py') info
    }
    elseif ($Command -eq 'delete-webhook') {
        & py (Join-Path $PSScriptRoot 'manage_webhook.py') delete
    }
    else {
        & py (Join-Path $PSScriptRoot 'telegram_insights.py') $Command
    }
    if ($LASTEXITCODE -ne 0) { throw "Telegram command failed with exit code $LASTEXITCODE." }
}
finally {
    Remove-Item Env:TELEGRAM_BOT_TOKEN -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
