[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '0.1.0',

    [Parameter(Mandatory = $false)]
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseDir = Join-Path $projectRoot 'release'
$workDir = Join-Path $projectRoot 'build\pyinstaller'
$specDir = Join-Path $projectRoot 'build\spec'
$artifactName = "LOL-Support-Advisor-v$Version"

New-Item -ItemType Directory -Force -Path $releaseDir, $workDir, $specDir | Out-Null

Push-Location $projectRoot
try {
    $packageVersion = (& $Python -c "from lol_support_advisor import __version__; print(__version__)" | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not read the package version.'
    }
    if ($packageVersion -ne $Version) {
        throw "Requested version $Version does not match package version $packageVersion."
    }

    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name $artifactName `
        --distpath $releaseDir `
        --workpath $workDir `
        --specpath $specDir `
        (Join-Path $projectRoot 'app.py')
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

$executable = Join-Path $releaseDir "$artifactName.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Release executable was not created: $executable"
}

$digest = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
$checksumPath = Join-Path $releaseDir 'SHA256SUMS.txt'
Set-Content -LiteralPath $checksumPath -Value "$digest *$artifactName.exe" -Encoding ascii

Write-Output "Built: $executable"
Write-Output "SHA256: $digest"
