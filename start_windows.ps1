param([switch]$SetupOllamaOnly)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$projectDir = $PSScriptRoot
Set-Location -LiteralPath $projectDir
$runtimeDir = Join-Path $projectDir '.runtime'
$pythonDir = Join-Path $runtimeDir 'python'
$pythonExe = Join-Path $pythonDir 'python.exe'
$dataDir = Join-Path $projectDir 'data'
$passwordFile = Join-Path $dataDir 'admin-password.txt'
$model = 'qwen3:0.6b'
$baseUrl = 'http://127.0.0.1:8787'

function Get-PortablePython {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'This launcher needs 64-bit Windows.'
    }
    New-Item -ItemType Directory -Path $pythonDir -Force | Out-Null
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        $downloadPath = Join-Path $runtimeDir 'python-3.12.10-embeddable-amd64.zip'
        $url = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embeddable-amd64.zip'
        $expectedHash = '156c7eea90d58cd7e91a23f28a0056616b13e9f4cf4901b7b99b837b7848c6da'
        Write-Host 'Downloading portable Python from python.org...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $downloadPath -UseBasicParsing
        $actualHash = (Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) {
            Remove-Item -LiteralPath $downloadPath -Force
            throw 'Python download checksum mismatch. Please retry.'
        }
        Expand-Archive -LiteralPath $downloadPath -DestinationPath $pythonDir -Force
        Remove-Item -LiteralPath $downloadPath -Force
    }
    # The embedded Python isolates imports. Add the project root for -m repostbot.app.
    $pth = Join-Path $pythonDir 'python312._pth'
    Set-Content -LiteralPath $pth -Encoding ASCII -Value @('python312.zip', '.', '..\..', '..\packages', 'import site')
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        throw 'Portable Python extraction did not produce python.exe.'
    }
    & $pythonExe -c 'import sqlite3, ssl, urllib.request' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Portable Python failed the self-check.'
    }
    return $pythonExe
}

function Get-ChatGPTDependencies {
    & $pythonExe -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('jwt') and importlib.util.find_spec('cryptography') else 1)" | Out-Null
    if ($LASTEXITCODE -eq 0) { return }
    $pipArchive = Join-Path $runtimeDir 'pip.pyz'
    $packagesDir = Join-Path $runtimeDir 'packages'
    try {
        Write-Host 'Installing ChatGPT sign-in support (one-time, no local model)...'
        if (-not (Test-Path -LiteralPath $pipArchive)) {
            Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/pip/pip.pyz' -OutFile $pipArchive -UseBasicParsing
        }
        & $pythonExe $pipArchive install --disable-pip-version-check --no-warn-script-location --target $packagesDir 'PyJWT[crypto]>=2.10.1,<3'
        if ($LASTEXITCODE -ne 0) { throw 'ChatGPT dependencies could not be installed.' }
    } catch {
        Write-Warning ('ChatGPT sign-in setup: ' + $_.Exception.Message + '. The bot can still start; retry the launcher later.')
    }
}

function Get-PDFDependencies {
    & $pythonExe -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('pypdf') else 1)" | Out-Null
    if ($LASTEXITCODE -eq 0) { return }
    $pipArchive = Join-Path $runtimeDir 'pip.pyz'
    $packagesDir = Join-Path $runtimeDir 'packages'
    try {
        if (-not (Test-Path -LiteralPath $pipArchive)) {
            Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/pip/pip.pyz' -OutFile $pipArchive -UseBasicParsing
        }
        & $pythonExe $pipArchive install --disable-pip-version-check --no-warn-script-location --target $packagesDir 'pypdf>=5,<7'
        if ($LASTEXITCODE -ne 0) { throw 'Could not install pypdf.' }
    } catch {
        Write-Warning ('PDF reading: ' + $_.Exception.Message + '. Other attachments still work.')
    }
}

function Get-AdminPassword {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    if (Test-Path -LiteralPath $passwordFile) {
        $saved = (Get-Content -LiteralPath $passwordFile -Raw).Trim()
        if ($saved.Length -ge 12) { return $saved }
    }
    $bytes = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $generated = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    Set-Content -LiteralPath $passwordFile -Encoding ASCII -Value $generated
    return $generated
}

function Test-BotWeb {
    try {
        $response = Invoke-WebRequest -Uri ($baseUrl + '/api/session') -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -eq 200 -and [string]$response.Headers['Server'] -like 'RepostBot/*')
    } catch { return $false }
}

function Test-OllamaApi {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:11434/api/tags' -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -eq 200)
    } catch { return $false }
}

function Find-Ollama {
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
        (Join-Path $env:LOCALAPPDATA 'Ollama\ollama.exe'),
        (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Setup-Ollama {
    $ollamaExe = Find-Ollama
    if (-not $ollamaExe -and -not (Test-OllamaApi)) {
        Write-Host 'Installing Ollama from ollama.com... This may take a few minutes.'
        $installer = Join-Path $runtimeDir 'ollama-install.ps1'
        Invoke-WebRequest -Uri 'https://ollama.com/install.ps1' -OutFile $installer -UseBasicParsing
        $installResult = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $installer + '"')) -Wait -PassThru
        if ($installResult.ExitCode -ne 0) { throw "Ollama installer exit code: $($installResult.ExitCode)" }
        $ollamaExe = Find-Ollama
    }
    if (-not (Test-OllamaApi)) {
        if (-not $ollamaExe) { throw 'Ollama executable was not found after installation.' }
        Write-Host 'Starting Ollama...'
        Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden | Out-Null
        for ($i = 0; $i -lt 30; $i++) {
            if (Test-OllamaApi) { break }
            Start-Sleep -Seconds 1
        }
    }
    if (-not (Test-OllamaApi)) { throw 'Ollama API did not start on 127.0.0.1:11434.' }
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
    $compatible = @($tags.models | Where-Object { $_.name -match '^qwen3:0\.6b(?:-q4_k_m)?$' })
    if ($compatible.Count -eq 0) {
        Write-Host 'Downloading the local model (about 523 MB)...'
        if ($ollamaExe) {
            & $ollamaExe pull $model
            if ($LASTEXITCODE -ne 0) { throw 'Could not download the model.' }
        } else {
            $payload = @{ model = $model; stream = $false } | ConvertTo-Json -Compress
            Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:11434/api/pull' -Body $payload -ContentType 'application/json' -TimeoutSec 1800 | Out-Null
        }
        $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
        $compatible = @($tags.models | Where-Object { $_.name -match '^qwen3:0\.6b(?:-q4_k_m)?$' })
        if ($compatible.Count -eq 0) { throw 'Ollama finished but the model is not listed in /api/tags.' }
    }
    $model = $compatible[0].name
    $marker = Join-Path $runtimeDir 'llm-configured.txt'
    $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    $headers = @{ 'X-Repostbot-Request' = '1' }
    $loginBody = @{ password = $env:ADMIN_PASSWORD } | ConvertTo-Json -Compress
    Invoke-RestMethod -Method Post -Uri ($baseUrl + '/api/login') -Body $loginBody -ContentType 'application/json' -Headers $headers -WebSession $session | Out-Null
    $settings = (Invoke-RestMethod -Uri ($baseUrl + '/api/config') -WebSession $session).settings
    if (-not (Test-Path -LiteralPath $marker)) {
        $settingsBody = @{ ollama_url = 'http://127.0.0.1:11434'; ollama_model = $model } | ConvertTo-Json -Compress
        Invoke-RestMethod -Method Post -Uri ($baseUrl + '/api/settings') -Body $settingsBody -ContentType 'application/json' -Headers $headers -WebSession $session | Out-Null
        Set-Content -LiteralPath $marker -Encoding ASCII -Value $model
    } elseif ($settings.ollama_model -match '^qwen3:0\.6b(?:-q4_k_m)?$' -and $settings.ollama_model -ne $model) {
        $settingsBody = @{ ollama_model = $model } | ConvertTo-Json -Compress
        Invoke-RestMethod -Method Post -Uri ($baseUrl + '/api/settings') -Body $settingsBody -ContentType 'application/json' -Headers $headers -WebSession $session | Out-Null
        Set-Content -LiteralPath $marker -Encoding ASCII -Value $model
    }
    Write-Host 'Ollama and the local model are ready.'
}

function Apply-RequestedUpdate {
    param([switch]$Force)
    $marker = Join-Path $dataDir 'updates\apply-requested.txt'
    $pending = Join-Path $dataDir 'updates\pending.zip'
    if (-not $Force -and -not (Test-Path -LiteralPath $marker)) { return $false }
    if (Test-Path -LiteralPath $marker) {
        $requested = (Get-Content -LiteralPath $marker -Raw).Trim()
        $installed = (Get-Content -LiteralPath (Join-Path $projectDir 'version.json') -Raw | ConvertFrom-Json).version
        if ($requested -and [version]$requested -le [version]$installed) {
            Remove-Item -LiteralPath $marker -Force
            Write-Host "Version $installed is already installed; starting the bot."
            return $false
        }
    }
    if (-not (Test-Path -LiteralPath $pending)) {
        if ($Force) { throw 'Update archive is missing from data\updates\pending.zip.' }
        Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
        return $false
    }
    Write-Host 'Applying the requested update...'
    & $pythonExe -m repostbot.update apply $dataDir
    if ($LASTEXITCODE -ne 0) { throw 'Update failed. The archive is still in data\updates\pending.zip.' }
    return $true
}

if ($SetupOllamaOnly) {
    try {
        $env:ADMIN_PASSWORD = Get-AdminPassword
        Setup-Ollama
        exit 0
    } catch {
        Write-Warning ('Ollama setup failed: ' + $_.Exception.Message + '. The bot is still available; try starting again later.')
        exit 1
    }
}

try {
    $pythonExe = Get-PortablePython
    $env:ADMIN_PASSWORD = Get-AdminPassword
    $env:DATA_DIR = $dataDir
    $env:WEB_HOST = '127.0.0.1'
    $env:WEB_PORT = '8787'
    $env:REPOSTBOT_WINDOWS_LAUNCHER = '1'
    if ($env:ADMIN_PASSWORD.Length -lt 12) { throw 'The admin password file is invalid.' }
    Write-Host ''
    Write-Host 'RepostBot Windows launcher'
    Write-Host ('Admin password: ' + $env:ADMIN_PASSWORD)
    Write-Host ('The password is also saved in: ' + $passwordFile)
    Apply-RequestedUpdate | Out-Null
    $startedHere = $false
    $botProcess = $null
    $setupJob = $null
    if (Test-BotWeb) {
        Write-Host 'The bot is already running. Opening its web panel.'
    } else {
        Write-Host 'Starting the bot...'
        $botProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'repostbot.app') -WorkingDirectory $projectDir -NoNewWindow -PassThru
        $startedHere = $true
        for ($i = 0; $i -lt 30; $i++) {
            if (Test-BotWeb) { break }
            if ($botProcess.HasExited) { throw "Bot exited with code $($botProcess.ExitCode)." }
            Start-Sleep -Seconds 1
        }
        if (-not (Test-BotWeb)) { throw 'The web panel did not start. Check whether port 8787 is in use.' }
    }
    try { Start-Process $baseUrl | Out-Null } catch { Write-Warning 'Could not open the browser automatically. Open the URL below manually.' }
    Write-Host ('Web panel: ' + $baseUrl)
    if ($startedHere) {
        Get-ChatGPTDependencies
        Get-PDFDependencies
        try {
            $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
            $headers = @{ 'X-Repostbot-Request' = '1' }
            $loginBody = @{ password = $env:ADMIN_PASSWORD } | ConvertTo-Json -Compress
            Invoke-RestMethod -Method Post -Uri ($baseUrl + '/api/login') -Body $loginBody -ContentType 'application/json' -Headers $headers -WebSession $session | Out-Null
            $settings = (Invoke-RestMethod -Uri ($baseUrl + '/api/config') -WebSession $session).settings
            if ($settings.ai_provider -eq 'local') {
                $setupJob = Start-Job -ScriptBlock {
                    param($scriptFile)
                    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $scriptFile -SetupOllamaOnly
                } -ArgumentList $PSCommandPath
            }
        } catch {
            Write-Warning ('Could not start Ollama setup: ' + $_.Exception.Message)
        }
        Write-Host 'Keep this window open while the bot runs. Close it or press Ctrl+C to stop.'
        while ($true) {
            while (-not $botProcess.HasExited) {
                if ($setupJob -and $setupJob.HasMoreData) { Receive-Job -Job $setupJob -ErrorAction Continue }
                if ($setupJob -and $setupJob.State -in @('Completed', 'Failed', 'Stopped')) {
                    Receive-Job -Job $setupJob -ErrorAction Continue
                    Remove-Job -Job $setupJob -Force -ErrorAction SilentlyContinue
                    $setupJob = $null
                }
                Start-Sleep -Milliseconds 500
            }
            $marker = Join-Path $dataDir 'updates\apply-requested.txt'
            if ($botProcess.ExitCode -ne 42 -and -not (Test-Path -LiteralPath $marker)) {
                throw "Bot stopped with code $($botProcess.ExitCode). Read the messages above and restart with start_windows.bat."
            }
            Apply-RequestedUpdate -Force | Out-Null
            $botProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'repostbot.app') -WorkingDirectory $projectDir -NoNewWindow -PassThru
            for ($i = 0; $i -lt 30; $i++) {
                if (Test-BotWeb) { break }
                if ($botProcess.HasExited) { throw "Updated bot exited with code $($botProcess.ExitCode)." }
                Start-Sleep -Seconds 1
            }
            if (-not (Test-BotWeb)) { throw 'Updated bot did not start.' }
            Write-Host 'Update installed. Web panel is available again.'
        }
    }
} catch {
    [Console]::Error.WriteLine('Startup failed: ' + $_.Exception.Message)
    exit 1
} finally {
    if ($setupJob) {
        Stop-Job -Job $setupJob -ErrorAction SilentlyContinue
        Remove-Job -Job $setupJob -Force -ErrorAction SilentlyContinue
    }
    if ($startedHere -and $botProcess -and -not $botProcess.HasExited) {
        Stop-Process -Id $botProcess.Id -ErrorAction SilentlyContinue
    }
}
