# Builds static/css/site.css with the Tailwind standalone CLI.
# There is no Node buildpack on Heroku, so the compiled CSS is committed to git.
# CI fails the build if the committed file is stale -- always run this before pushing.
#
#   pwsh scripts/build_css.ps1            # one-off build
#   pwsh scripts/build_css.ps1 -Watch     # rebuild on change while developing

param([switch]$Watch)

$ErrorActionPreference = "Stop"

# Keep this in sync with TAILWIND_VERSION in .github/workflows/heroku-deploy.yml
$Version = "v4.3.3"
$Root = Split-Path -Parent $PSScriptRoot
$ToolsDir = Join-Path $Root ".tools"
$Binary = Join-Path $ToolsDir "tailwindcss-$Version.exe"

if (-not (Test-Path $Binary)) {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    $Url = "https://github.com/tailwindlabs/tailwindcss/releases/download/$Version/tailwindcss-windows-x64.exe"
    Write-Host "Downloading Tailwind CLI $Version ..."
    # curl.exe, not Invoke-WebRequest: the latter truncated this ~110 MB asset.
    curl.exe -sSL -o $Binary $Url
    if ($LASTEXITCODE -ne 0) { throw "Tailwind CLI download failed." }
}

$InputPath = Join-Path $Root "static\src\input.css"
$OutputPath = Join-Path $Root "static\css\site.css"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null

# NOTE: $Input and $Args are PowerShell automatic variables -- do not reuse those names.
$CliArgs = @("-i", $InputPath, "-o", $OutputPath, "--minify")
if ($Watch) { $CliArgs += "--watch" }

Push-Location $Root
try {
    & $Binary @CliArgs
}
finally {
    Pop-Location
}
