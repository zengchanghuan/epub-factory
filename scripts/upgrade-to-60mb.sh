#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/upgrade-to-60mb.sh
#
# 把单文件上传上限从 20MB 调到 60MB 的"运维侧"一键升级脚本。
# 适用于 2C2G/3Mbps 的轻量服务器或同等小机型。
#
# 这个脚本只做"机器侧"的 3 件事；代码侧的改动（celery 并发收敛、前端文案）
# 已经在仓库里完成，部署时正常 git pull + 重启即可。
#
#   1. 写入 backend/.env 中的 4 个环境变量（幂等，已存在则覆盖）
#   2. 把 nginx 的 client_max_body_size 调到 80M（留 20M 缓冲）
#   3. 创建 2GiB swap 文件（如果尚未存在），防 OOM 兜底
#
# 用法：
#   sudo bash scripts/upgrade-to-60mb.sh                   # 默认值
#   sudo MAX_FILE_SIZE_MB=80 bash scripts/upgrade-to-60mb.sh   # 自定义
#
# 设计原则（五大支柱对照）：
#   - 失败可回滚：所有写入都做 .bak 备份；nginx 改动前 nginx -t 验证
#   - 成本可预测：纯免费的运维操作，不动机器配置
#   - 数据可控  ：所有阈值通过环境变量传入，不硬编码
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── 可调参数（通过环境变量覆盖）────────────────────────────────────────────
MAX_FILE_SIZE_MB="${MAX_FILE_SIZE_MB:-60}"
NGINX_BODY_SIZE_MB="${NGINX_BODY_SIZE_MB:-80}"   # 比应用层多留缓冲
SWAP_SIZE_GB="${SWAP_SIZE_GB:-2}"
CELERY_WORKER_CONCURRENCY="${CELERY_WORKER_CONCURRENCY:-1}"
CELERY_TASK_TIME_LIMIT="${CELERY_TASK_TIME_LIMIT:-1800}"
CELERY_TASK_SOFT_TIME_LIMIT="${CELERY_TASK_SOFT_TIME_LIMIT:-1500}"

# 项目目录：默认按本脚本所在位置反推（scripts/ 的父目录就是仓库根）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(dirname "${SCRIPT_DIR}")}"
ENV_FILE="${PROJECT_ROOT}/backend/.env"

# ── 工具函数 ─────────────────────────────────────────────────────────────
log()  { echo -e "\033[1;32m[upgrade] $*\033[0m"; }
warn() { echo -e "\033[1;33m[warn] $*\033[0m"; }
err()  { echo -e "\033[1;31m[error] $*\033[0m" >&2; }

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        err "需要 root 权限，请用 sudo 运行：sudo bash $0"
        exit 1
    fi
}

# 写入或更新 .env 中的某一行（key=value），保持幂等
upsert_env() {
    local key="$1" val="$2" file="$3"
    [[ -f "$file" ]] || touch "$file"
    if grep -qE "^[#[:space:]]*${key}=" "$file"; then
        sed -i.bak -E "s|^[#[:space:]]*${key}=.*|${key}=${val}|" "$file"
    else
        echo "${key}=${val}" >> "$file"
    fi
}

# ── 步骤 1：写入 backend/.env ──────────────────────────────────────────────
step_env() {
    log "step 1/3 — 写入 backend/.env 的资源上限配置"
    if [[ ! -d "$(dirname "${ENV_FILE}")" ]]; then
        err "找不到目录 $(dirname "${ENV_FILE}")，请通过 PROJECT_ROOT=/path/to/repo 显式指定"
        exit 1
    fi
    upsert_env "MAX_FILE_SIZE_MB" "${MAX_FILE_SIZE_MB}" "${ENV_FILE}"
    upsert_env "CELERY_WORKER_CONCURRENCY" "${CELERY_WORKER_CONCURRENCY}" "${ENV_FILE}"
    upsert_env "CELERY_TASK_TIME_LIMIT" "${CELERY_TASK_TIME_LIMIT}" "${ENV_FILE}"
    upsert_env "CELERY_TASK_SOFT_TIME_LIMIT" "${CELERY_TASK_SOFT_TIME_LIMIT}" "${ENV_FILE}"
    log "  → MAX_FILE_SIZE_MB=${MAX_FILE_SIZE_MB}"
    log "  → CELERY_WORKER_CONCURRENCY=${CELERY_WORKER_CONCURRENCY}"
    log "  → CELERY_TASK_TIME_LIMIT=${CELERY_TASK_TIME_LIMIT}s"
    log "  → CELERY_TASK_SOFT_TIME_LIMIT=${CELERY_TASK_SOFT_TIME_LIMIT}s"
}

# ── 步骤 2：调整 Nginx client_max_body_size ───────────────────────────────
step_nginx() {
    log "step 2/3 — 调整 Nginx client_max_body_size 到 ${NGINX_BODY_SIZE_MB}M"
    if ! command -v nginx >/dev/null 2>&1; then
        warn "  未检测到 nginx，跳过这一步（如果你不用 nginx 反代可以忽略）"
        return
    fi

    local conf=""
    for candidate in \
        /etc/nginx/sites-enabled/fixepub \
        /etc/nginx/sites-enabled/default \
        /etc/nginx/conf.d/fixepub.conf \
        /etc/nginx/conf.d/default.conf; do
        if [[ -f "$candidate" ]]; then
            conf="$candidate"
            break
        fi
    done

    if [[ -z "$conf" ]]; then
        warn "  没有找到常见路径下的 nginx 站点配置，请手动加上："
        echo "    client_max_body_size ${NGINX_BODY_SIZE_MB}M;"
        echo "    client_body_timeout 180s;"
        echo "    proxy_read_timeout 1800s;"
        return
    fi
    log "  发现配置文件: ${conf}"
    cp -a "$conf" "${conf}.bak.$(date +%Y%m%d-%H%M%S)"

    if grep -qE "client_max_body_size" "$conf"; then
        sed -i -E "s|client_max_body_size[[:space:]]+[^;]+;|client_max_body_size ${NGINX_BODY_SIZE_MB}M;|" "$conf"
        log "  已更新已有 client_max_body_size 行"
    else
        # 在第一个 server { 之后插入 3 行配置
        sed -i -E "0,/server[[:space:]]*\{/s||server {\n    client_max_body_size ${NGINX_BODY_SIZE_MB}M;\n    client_body_timeout 180s;\n    proxy_read_timeout 1800s;|" "$conf"
        log "  已新增 client_max_body_size / client_body_timeout / proxy_read_timeout"
    fi

    if nginx -t 2>&1; then
        systemctl reload nginx
        log "  nginx 配置校验通过并已 reload"
    else
        err "  nginx -t 校验失败！已自动回滚备份"
        latest_bak="$(ls -t "${conf}".bak.* 2>/dev/null | head -1 || true)"
        [[ -n "$latest_bak" ]] && cp -a "$latest_bak" "$conf"
        exit 1
    fi
}

# ── 步骤 3：创建 swap 文件 ─────────────────────────────────────────────────
step_swap() {
    log "step 3/3 — 创建 ${SWAP_SIZE_GB}GiB swap 防 OOM 兜底"
    if swapon --show 2>/dev/null | grep -qE "^/swapfile"; then
        log "  /swapfile 已存在并启用，跳过"
        return
    fi

    if [[ -f /swapfile ]]; then
        warn "  /swapfile 文件存在但未启用，尝试启用"
    else
        fallocate -l "${SWAP_SIZE_GB}G" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_SIZE_GB*1024))
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
    fi

    swapon /swapfile
    if ! grep -qE "^/swapfile " /etc/fstab; then
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    sysctl -w vm.swappiness=10 >/dev/null
    if ! grep -qE "^vm.swappiness" /etc/sysctl.conf; then
        echo 'vm.swappiness=10' >> /etc/sysctl.conf
    fi
    log "  swap 启用完毕，当前内存状态："
    free -h | sed 's/^/    /'
}

# ── 主流程 ───────────────────────────────────────────────────────────────
main() {
    require_root
    log "===== 升级到 ${MAX_FILE_SIZE_MB}MB 单文件上限 ====="
    log "项目目录: ${PROJECT_ROOT}"
    step_env
    step_nginx
    step_swap

    cat <<EOF

============================================================
  ✓ 升级完成

  剩余动作（手动）：
    1. cd ${PROJECT_ROOT} && git pull           # 拉取代码侧改动
    2. systemctl restart epub-factory.service   # 重启 API
    3. systemctl restart celery 或对应 systemd 单元

  验证：
    - 上传一个 50MB 文件，应能成功
    - free -h 应能看到 Swap: 大约 ${SWAP_SIZE_GB}.0Gi
    - sudo nginx -T | grep client_max_body_size 应显示 ${NGINX_BODY_SIZE_MB}M
============================================================
EOF
}

main "$@"
