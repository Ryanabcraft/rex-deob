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

# msys/git-bash python builds a POSIX venv (.venv/bin); the Windows launcher
# builds .venv/Scripts. Prefer the launcher so the venv lands where everything
# else expects it.
$launcher = if (Get-Command py -ErrorAction SilentlyContinue) { @("py", "-3") }
            else { @("python") }

Write-Host "`n== python venv ==" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) { & $launcher[0] @($launcher[1..($launcher.Count - 1)]) -m venv .venv }

$venvPy = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" }
          elseif (Test-Path ".venv\bin\python.exe") { ".venv\bin\python.exe" }
          else { throw "venv python not found in .venv\Scripts or .venv\bin" }

& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r requirements.txt --quiet

Write-Host "`n== engines ==" -ForegroundColor Cyan
& $venvPy cli.py --engines

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "`n.env created - paste DISCORD_TOKEN into it" -ForegroundColor Yellow
}
Write-Host "`nrun the bot:" -ForegroundColor Cyan
Write-Host "  $venvPy bot.py"
