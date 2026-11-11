param([string]$Python = 'python', [string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
$appDirectory = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $PSScriptRoot 'dist' }
$stagingDirectory = Join-Path $PSScriptRoot 'build\release'

Push-Location $PSScriptRoot

try {

    & $Python -m PyInstaller --noconfirm --distpath $stagingDirectory --workpath 'build\pyinstaller' 'ZLG_Scope.spec'

    if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }

    $application = Join-Path $stagingDirectory 'ZLG示波器上位机.exe'

    $report = Join-Path $PSScriptRoot 'tmp\build_validation\packaged_startup.json'

    if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report }

    $check = Start-Process -FilePath $application -ArgumentList '--self-test-report', ('"' + $report + '"') -Wait -PassThru -WindowStyle Hidden

    if ($check.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $report)) {

        throw 'Packaged startup check failed. See build\release\data\logs\application.log.'

    }

    $result = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json

    if (-not $result.success -or -not $result.frozen) { throw 'Invalid packaged startup report.' }

    New-Item -ItemType Directory -Path $appDirectory -Force | Out-Null

    $destination = Join-Path $appDirectory 'ZLG示波器上位机.exe'

    Copy-Item -LiteralPath $application -Destination $destination -Force

    if ((Get-FileHash -LiteralPath $application).Hash -ne (Get-FileHash -LiteralPath $destination).Hash) {

        throw 'Published EXE does not match verified build.'

    }

    Write-Host "Ready: $destination"

} finally {

    Pop-Location

}

