$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env.local'))) {
    throw '.env.local is missing. Configure local Supabase credentials first.'
}
& py (Join-Path $PSScriptRoot 'migrate_archive.py')
if ($LASTEXITCODE -ne 0) { throw 'Archive migration failed.' }
