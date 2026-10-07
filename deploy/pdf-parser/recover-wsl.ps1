param(
    [string]$Distribution = 'Ubuntu-24.04',
    [string]$DeploymentRoot = '/opt/docker/vllm/ocr-server'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ($Distribution -notmatch '^[a-zA-Z0-9._-]+$' -or $DeploymentRoot -notmatch '^/opt/[a-zA-Z0-9/_-]+$') {
    throw 'Invalid recovery target'
}
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'paused')) { exit 0 }

$log = Join-Path $PSScriptRoot 'recovery.log'
if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 1MB) {
    Move-Item -LiteralPath $log -Destination (Join-Path $PSScriptRoot 'recovery.previous.log') -Force
}
$exitCode = 1
$status = 'wsl_launch_failed'
try {
    $result = & "$env:SystemRoot\System32\wsl.exe" -d $Distribution -u root --exec bash "$DeploymentRoot/deploy/pdf-parser/recover.sh" 2>&1
    $exitCode = $LASTEXITCODE
    # Log only controlled status messages, never raw configuration or errors.
    $statuses = @($result | ForEach-Object { "$_" -replace "`0", '' } | Where-Object {
        $_ -match '^(healthy|recovered_healthy|recovery_busy|maintenance_paused|docker_start_failed|compose_config_invalid|recovery_cooldown|unhealthy_restart_failed|recovery_failed_check_container_logs|(warming_up|manual_attention|container_missing_or_ambiguous):(ocr-vllm|pdf-parser))$'
    })
    if ($statuses.Count -gt 0) { $status = $statuses[-1] }
} catch {
    $status = 'wsl_launch_failed'
}
Add-Content -LiteralPath $log -Value ('{0} status={1} exit={2}' -f [DateTimeOffset]::Now.ToString('o'), $status, $exitCode) -Encoding UTF8
exit $exitCode
