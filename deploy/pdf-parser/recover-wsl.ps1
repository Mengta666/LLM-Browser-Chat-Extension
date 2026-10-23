<#
.SYNOPSIS
在指定 WSL 发行版中运行 OCR 恢复脚本，并记录受控状态。
.DESCRIPTION
支持暂停标记与日志轮换；可恢复指定 IPv4 地址到本机 OCR 端口的转发，不修改防火墙。
只记录白名单状态，不写入子进程的原始配置或错误输出。
#>
param(
    [string]$Distribution = 'Ubuntu-24.04',
    [string]$DeploymentRoot = '/opt/docker/vllm/ocr-server',
    [string]$ListenAddress = '',
    [ValidateRange(1, 65535)][int]$ListenPort = 19082
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ($Distribution -notmatch '^[a-zA-Z0-9._-]+$' -or $DeploymentRoot -notmatch '^/opt/[a-zA-Z0-9/_-]+$') {
    throw 'Invalid recovery target'
}
if ($ListenAddress) {
    $address = $null
    if (![Net.IPAddress]::TryParse($ListenAddress, [ref]$address) -or
        $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        $ListenAddress -eq '0.0.0.0' -or [Net.IPAddress]::IsLoopback($address)) {
        throw 'ListenAddress must be a specific non-loopback IPv4 address'
    }
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
    # 只保留白名单状态，防止子进程的原始错误或配置内容进入日志。
    $statuses = @($result | ForEach-Object { "$_" -replace "`0", '' } | Where-Object {
        $_ -match '^(healthy|recovered_healthy|recovery_busy|maintenance_paused|docker_start_failed|compose_config_invalid|recovery_cooldown|unhealthy_restart_failed|recovery_failed_check_container_logs|(warming_up|manual_attention|container_missing_or_ambiguous):(ocr-vllm|pdf-parser))$'
    })
    if ($statuses.Count -gt 0) { $status = $statuses[-1] }
} catch {
    $status = 'wsl_launch_failed'
}
$networkStatus = 'not_configured'
if ($ListenAddress) {
    $networkStatus = 'waiting_for_ocr'
    if ($exitCode -eq 0 -and $status -in @('healthy', 'recovered_healthy')) {
        try {
            # 开机时 VPN 可能晚于计划任务就绪；保留配置，交给下一次巡检重试。
            $localAddress = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $ListenAddress -ErrorAction SilentlyContinue
            if (!$localAddress) {
                $networkStatus = 'address_pending'
                $exitCode = 1
            } else {
                $networkStatus = 'loopback_unhealthy'
                $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ListenPort/health" -TimeoutSec 5
                if ($health.status -ne 'ok' -or $health.service -ne 'pdf-parser') { throw 'Unexpected loopback health' }
                $networkStatus = 'portproxy_repair_failed'
                $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $ListenPort -ErrorAction SilentlyContinue |
                    Where-Object { $_.LocalAddress -eq $ListenAddress })
                $repaired = $false
                if ($listeners.Count -eq 0) {
                    # 持久化规则存在不代表实际监听存在；set 无法修复时，重新注册这一条规则。
                    # 不重启共享 IP Helper 服务，也不重置其他端口转发或扩大防火墙范围。
                    & "$env:SystemRoot\System32\netsh.exe" interface portproxy delete v4tov4 "listenaddress=$ListenAddress" "listenport=$ListenPort" protocol=tcp 2>&1 | Out-Null
                    & "$env:SystemRoot\System32\netsh.exe" interface portproxy add v4tov4 "listenaddress=$ListenAddress" "listenport=$ListenPort" connectaddress=127.0.0.1 "connectport=$ListenPort" protocol=tcp 2>&1 | Out-Null
                    if ($LASTEXITCODE -ne 0) { throw 'Portproxy registration failed' }
                    $repaired = $true
                    Start-Sleep -Seconds 2
                }
                $networkStatus = 'forward_health_failed'
                $health = Invoke-RestMethod -Uri "http://${ListenAddress}:$ListenPort/health" -TimeoutSec 5
                if ($health.status -ne 'ok' -or $health.service -ne 'pdf-parser') { throw 'Unexpected forwarded health' }
                $networkStatus = if ($repaired) { 'recovered' } else { 'healthy' }
            }
        } catch {
            $exitCode = 1
        }
    }
}
Add-Content -LiteralPath $log -Value ('{0} status={1} network={2} exit={3}' -f [DateTimeOffset]::Now.ToString('o'), $status, $networkStatus, $exitCode) -Encoding UTF8
exit $exitCode
