#Requires -Version 5.1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Check($name, $cmd) {
    $p = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($p) { Write-Host ("  [ok]   {0,-10} {1}" -f $name, $p.Source) -ForegroundColor Green }
    else    { Write-Host ("  [miss] {0,-10} install it to enable its engines" -f $name) -ForegroundColor Yellow }
}

Write-Host "== checking runtimes ==" -ForegroundColor Cyan
Check "python" "python"
Check "node"   "node"
Check "java"   "java"
Check "dotnet" "dotnet"

Write-Host "`n== python venv ==" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) { python -m venv .venv }
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet

Write-Host "`n== engines ==" -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" cli.py --engines

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "`n.env created - paste DISCORD_TOKEN into it" -ForegroundColor Yellow
}
Write-Host "`nrun the bot:" -ForegroundColor Cyan
Write-Host "  .\.venv\Scripts\python.exe bot.py"
