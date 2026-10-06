#!/usr/bin/env bash
set -euo pipefail

# 远程部署入口。由 GitHub Actions（.github/workflows/deploy-prod.yml）通过 SSH 调用：
#
#   /srv/mc-whitelist/deploy.sh [ref]
#
# 约定：
#   - 参数：要部署的 Git ref，默认 prod
#   - 幂等：可重复执行，重复跑同一 ref 不产生破坏性副作用
#   - 失败必须非 0 退出（set -e 已保证，但请勿自己吞掉错误）
#   - 本文件由仓库 deploy/deploy.sh 同步而来，不要在服务器上直接改；
#     改动请提交到仓库，再由 deploy-prod.yml 上传
#   - 运行时环境变量放在 /srv/mc-whitelist/app.env，不要写进本文件

REF="${1:-prod}"
APP_DIR="/srv/mc-whitelist"

cd "$APP_DIR"

echo "[deploy] ref=$REF dir=$APP_DIR"

# ===== 项目自定义部署逻辑 =====
# 在这里实现：拉取代码、安装依赖、构建、重启服务等
# ============================

echo "[deploy] done"
