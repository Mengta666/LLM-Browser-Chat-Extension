#!/usr/bin/env bash
set -euo pipefail
umask 077

deploy=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd -- "$deploy"
mkdir -p runtime/recovery
# 文件锁覆盖本次恢复全过程，避免定时任务与开机任务同时重启容器。
exec 9>runtime/recovery/recover.lock
flock -n 9 || { echo 'recovery_busy'; exit 0; }
if [[ -f runtime/recovery/paused ]]; then
    echo 'maintenance_paused'
    exit 0
fi

if ! timeout 10s docker info >/dev/null 2>&1; then
    if ! timeout 60s systemctl start docker >/dev/null 2>&1; then
        echo 'docker_start_failed'
        exit 1
    fi
fi

compose=(docker compose --env-file "$deploy/.env" -f "$deploy/compose.yaml" -f "$deploy/compose.recovery.yaml")
if ! timeout 20s "${compose[@]}" config --quiet >/dev/null 2>&1; then
    echo 'compose_config_invalid'
    exit 1
fi

repair=false
unhealthy=()
for service in ocr-vllm pdf-parser; do
    container=$(timeout 15s "${compose[@]}" ps -a -q "$service" 2>/dev/null) || exit 1
    # 缺失容器可能是主动卸载：不自动创建容器、拉取镜像或下载模型。
    if [[ ! "$container" =~ ^[a-f0-9]{12,64}$ ]]; then
        echo "container_missing_or_ambiguous:$service"
        exit 1
    fi
    state=$(timeout 10s docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null) || exit 1
    case "$state" in
        'running healthy') ;;
        'running starting'|'restarting '*)
            echo "warming_up:$service"
            exit 0
            ;;
        'running unhealthy')
            unhealthy+=("$container")
            repair=true
            ;;
        'exited '*|'created '*) repair=true ;;
        *) echo "manual_attention:$service"; exit 1 ;;
    esac
done

if [[ "$repair" == false ]]; then
    echo 'healthy'
    exit 0
fi

now=$(date +%s)
previous=0
if [[ -f runtime/recovery/last_attempt ]]; then
    read -r previous <runtime/recovery/last_attempt || true
fi
if [[ "$previous" =~ ^[0-9]+$ ]] && (( now >= previous && now - previous < 600 )); then
    echo 'recovery_cooldown'
    exit 1
fi
# 尝试前写入冷却时间；失败也进入冷却，避免持续故障导致密集重启。
printf '%s\n' "$now" >runtime/recovery/last_attempt

for container in "${unhealthy[@]}"; do
    if ! timeout 60s docker restart --time 30 "$container" >/dev/null 2>&1; then
        echo 'unhealthy_restart_failed'
        exit 1
    fi
done

# 只恢复现有部署并等待健康检查，不在恢复过程中构建、拉取镜像或重建容器。
if timeout -k 10s 920s "${compose[@]}" up -d --no-build --pull never --no-recreate --wait --wait-timeout 900 ocr-vllm pdf-parser >/dev/null 2>&1; then
    echo 'recovered_healthy'
else
    echo 'recovery_failed_check_container_logs'
    exit 1
fi
