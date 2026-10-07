param(
    [string]$Distribution = 'Ubuntu-24.04',
    [string]$DeploymentRoot = '/opt/docker/vllm/ocr-server'
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run elevated as the WSL distribution owner, not SYSTEM' }
if ($identity.User.Value -eq 'S-1-5-18') { throw 'SYSTEM does not own this WSL distribution' }
if ($Distribution -notmatch '^[a-zA-Z0-9._-]+$' -or $DeploymentRoot -notmatch '^/opt/[a-zA-Z0-9/_-]+$') { throw 'Invalid recovery target' }
$installed = @(& "$env:SystemRoot\System32\wsl.exe" --list --quiet | ForEach-Object { ($_ -replace "`0", '').Trim() })
if ($LASTEXITCODE -ne 0 -or $Distribution -notin $installed) { throw 'Distribution not registered for this Windows account' }

$taskName = 'Browser-Agent-OCR-Recovery'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Recovery task already exists; inspect it before replacing' }
$destination = Join-Path $env:ProgramData 'BrowserAgentOCR'
if (Test-Path -LiteralPath $destination) { throw 'Recovery directory already exists; inspect it before replacing' }
$source = Join-Path $PSScriptRoot 'recover-wsl.ps1'
if (!(Test-Path -LiteralPath $source -PathType Leaf)) { throw 'Missing recover-wsl.ps1' }
New-Item -ItemType Directory -Path $destination | Out-Null
& icacls.exe $destination /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' ("*{0}:(OI)(CI)F" -f $identity.User.Value) | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Recovery directory ACL failed' }
Copy-Item -LiteralPath $source -Destination (Join-Path $destination 'recover-wsl.ps1')

$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}\recover-wsl.ps1" -Distribution "{1}" -DeploymentRoot "{2}"' -f $destination, $Distribution, $DeploymentRoot
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $arguments
$boot = New-ScheduledTaskTrigger -AtStartup
$boot.Delay = 'PT30S'
$periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $identity.User.Value -LogonType S4U -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($boot, $periodic) -Principal $taskPrincipal -Settings $settings -Description 'Recover the existing browser-agent OCR containers in this user-owned WSL distribution; no image/model updates.' | Out-Null
Write-Output "Installed $taskName for $($identity.Name); no password stored."
