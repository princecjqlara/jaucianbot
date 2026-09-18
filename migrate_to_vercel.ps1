$ErrorActionPreference = 'Stop'
$secureUrl = Read-Host 'Paste the Postgres connection string from Vercel/Neon (input is hidden)' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureUrl)
try {
    $env:DATABASE_URL = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { $python = 'py' }
    & $python (Join-Path $PSScriptRoot 'migrate_archive.py')
    if ($LASTEXITCODE -ne 0) { throw 'Archive migration failed.' }
}
finally {
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
