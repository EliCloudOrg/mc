# mc 部署说明

本仓库使用「GitHub Actions 触发 + 服务器自管」的自托管部署框架。

框架只负责**触发和调用**，不关心项目怎么构建、怎么运行；一切具体部署逻辑都在服务器的
`/srv/mc/deploy.sh` 里。

## 部署架构

```
      feature/* ──PR──▶ main ──PR──▶ prod
                         │            │
                    push │            │ push / workflow_dispatch(ref)
                         ▼            ▼
              ┌──────────────────┐  ┌───────────────────────────────────────┐
              │ CI  ci.yml       │  │ Deploy  deploy-prod.yml               │
              │  检出            │  │  ① (可选) 构建镜像 → ghcr.io          │
              │  安装依赖        │  │     仅当 vars.DEPLOY_MODE == image    │
              │  测试            │  │  ② scp deploy/deploy.sh → /srv/mc│
              │  构建            │  │  ③ ssh 执行 /srv/mc/deploy.sh REF│
              └──────────────────┘  │  environment: production（人工审批）  │
                                    └──────────────────┬────────────────────┘
                                                       │ SSH（deploy 用户 + 私钥）
                                                       ▼
                                    ┌───────────────────────────────────────┐
                                    │ 服务器  /srv/mc/                 │
                                    │   deploy.sh   ← 随仓库版本管理         │
                                    │   app.env     ← 运行时环境变量（600）  │
                                    │   项目自己的构建产物 / 容器 / 进程     │
                                    └───────────────────────────────────────┘
```

## 分支模型

| 分支 | 用途 | 合并规则 |
|---|---|---|
| `main` | 集成分支，日常 PR 合入，跑 CI | 需要 PR + CI 通过 |
| `prod` | 发布分支，受保护 | 只允许从 `main` 发 PR 合并；合并即触发部署 |
| `feature/*` | 功能分支 | 从 `main` 拉出，PR 回 `main` |

## 首次部署

1. **服务器初始化**（root）
   ```bash
   scp scripts/bootstrap-server.sh ubuntu@146.56.237.33:/tmp/
   ssh ubuntu@146.56.237.33 'sudo bash /tmp/bootstrap-server.sh mc'
   ```
   脚本会创建 `deploy` 用户、`/srv/mc`（deploy:deploy 750）、空的
   `/srv/mc/app.env`，并把 deploy 加入 docker 组（若装了 Docker）。
   > 本机 **`docker-admin` 没有免密 sudo**，只有 `ubuntu` 有（`(ALL) NOPASSWD: ALL`），
   > 所以这一步必须用 `ubuntu` 账号。本机 `deploy` 用户**已存在且已在 `docker` 组**
   > （`uid=1008(deploy) groups=...,1004(docker)`，实测），脚本会跳过这两步。

2. **配置 SSH 免密**（按脚本最后打印的步骤）：生成部署专用密钥对，公钥写入
   `/home/deploy/.ssh/authorized_keys`，私钥内容写入 GitHub Secrets `SSH_KEY`。

3. **填写 GitHub 配置**：Secrets（`SSH_HOST`/`SSH_USER`/`SSH_KEY`/`SSH_PORT`）、
   Variables（`DEPLOY_MODE`）、Environment `production`（带 Required reviewers）、
   分支保护规则。具体值写入本文档下面的「GitHub 配置清单」。

4. **写实部署逻辑**：编辑仓库 `deploy/deploy.sh` 的「项目自定义部署逻辑」段，
   实现拉代码 / 装依赖 / 构建 / 重启服务。

5. **填充运行时环境变量**：在服务器上编辑 `/srv/mc/app.env`。

6. **首次发布**：GitHub → Actions → **Deploy to production** → *Run workflow*，
   `ref` 填 `prod`。人工审批通过后，Actions 会把 `deploy.sh` 上传到
   `/srv/mc/deploy.sh` 并执行。

## ✅ 部署逻辑已写实（`deploy.sh`）

框架约定 `deploy/deploy.sh` 保留骨架结构，项目特定逻辑补进「项目自定义部署逻辑」段。
本仓库**已完成**：`deploy/deploy.sh` 在保留骨架注释的同时实现了本项目所需的全部步骤 ——
snap 绕行（`/srv` 读变量、`/home` 交给 docker）、两张 external 网络预检、
`ghcr` 拉取带超时并回退本机镜像、一次性迁移旧的手工容器、
健康检查（含 RCON 可达性）与回环 `/healthz` 自检。

- **可运行副本**：[`docs/deploy-scenario.sh`](docs/deploy-scenario.sh)（与服务器上执行的是同一套逻辑，作为排错时的参照）。
- **已实测**：首次执行把容器从手工部署迁移到 compose 管理（`PROJECT=mc`），
  SQLite 33 条审计全部保留；镜像最终切到 `ghcr.io/elicloudorg/mc:prod`。

### 一次性迁移已完成（2026-10-06）

线上容器原先由**手工部署**产生，与框架形态有三处不同，迁移**已经执行完毕**：

| | 迁移前（手工） | 现在（框架化） |
|---|---|---|
| 项目目录 | `/home/docker-admin/elicloud/mc` | `/home/deploy/elicloud-mc` |
| compose 项目名 | 无（`docker run` 起的裸容器） | **`mc-svc`**（见下方「项目名冲突」一节；容器名仍是 `mc`） |
| 运行时变量 | 项目目录里的 `.env`（600） | `/srv/mc/app.env`（600, deploy:deploy） |
| 镜像 | 本地 `build` 的 `elicloud-mc:1.0.0` | `ghcr.io/elicloudorg/mc:prod` |

`deploy.sh` 第 3 步会自动识别「同名但不属于本 compose 项目的容器」并先移除，
所以这次迁移没有手工干预；**但 SQLite 数据必须人工搬**（脚本不碰数据）：

```bash
sudo cp -a /home/docker-admin/elicloud/mc/data/. /home/deploy/elicloud-mc/data/
```

> ⚠️ 这一步不能省：`mc.db` 里是**绑定关系与审计**。丢了它，MC 侧白名单还在，
> 但库里不认识那些名字（用户再申请同名会拿到 409 `name_taken`）。

## ⚠️ 项目名冲突（2026-10-06 实际事故，务必先读）

**MC 服务器那套栈的 compose 项目名就是 `mc`**（编排在 `/home/docker-admin/mc/docker-compose.yml`，
项目名由目录名推导），所以**本服务的 compose 项目名绝对不能叫 `mc`**。

事故经过：本项目最初设了 `COMPOSE_PROJECT_NAME=mc`。`docker compose up -d` 会**按项目名匹配
已有容器**，于是它把 MC 栈的 `urania-mc` 当成自己项目的容器，**用本项目的编排重建了它** ——
`urania-mc` 的 `/data` 从原来的匿名卷（真正的 world 与服务端文件）被换成了
`/home/deploy/elicloud-mc/data`，重启后因 `eula.txt` 写不进去（uid 1000 vs 目录属主 1002）
进入崩溃循环，MC 服务器一度不可用。

**现有约束（改名前请务必确认）**：

| 标识 | 值 | 为什么 |
|---|---|---|
| 服务 id / 容器名 / 镜像 / `/srv` 路径 | `mc` | 对外身份，网关与文档都按它引用 |
| **compose 项目名**（`COMPOSE_PROJECT_NAME`） | **`mc-svc`** | 避开 MC 栈的项目名 `mc`，否则会误重建别人的容器 |

> 另有两个容易混的名字：MC 栈的服务名也叫 `mc`（容器名是 `urania-mc`）；
> 而 `mc-backup-1` 的 `RCON_HOST=mc` 指的是**MC 栈自己的服务名**，与本服务无关。

`deploy.sh` 已加**执行前校验**：核对 `docker-compose.yml` 的服务名必须是 `mc`、
`container_name` 必须与预期一致，任一不符就直接失败——防止再次把别人的容器当自己的重建。


## GitHub 配置清单

> 下表在初始化时按仓库实际值填写（不能留示例值）。

### Secrets（Settings → Secrets and variables → Actions → Secrets）

| Secret | 值 | 说明 |
|---|---|---|
| `SSH_HOST` | `146.56.237.33` | 域名或 IP |
| `SSH_USER` | `deploy` | `bootstrap-server.sh` 创建的系统用户 |
| `SSH_PORT` | `22` | SSH 端口 |
| `SSH_KEY` | 部署私钥**全文**（含 `-----BEGIN/END ... KEY-----`） | 单独生成，不复用个人密钥；公钥放服务器 `/home/deploy/.ssh/authorized_keys` |
| `GHCR_TOKEN` | 一般不需要 | 仅跨仓库/外部 registry 推镜像时需要；同仓库推 `ghcr.io` 用内置 `GITHUB_TOKEN` |

```bash
gh secret set SSH_HOST --body '146.56.237.33'
gh secret set SSH_USER --body 'deploy'
gh secret set SSH_PORT --body '22'
gh secret set SSH_KEY < ./deploy_key      # 部署私钥文件
```

### Variables（同一页面的 Variables 标签）

| Variable | 值 | 说明 |
|---|---|---|
| `DEPLOY_MODE` | `image` 或留空/其它值 | `image` → 阶段一构建并推镜像；其它值 → 阶段一被跳过，只跑阶段二 |

```bash
gh variable set DEPLOY_MODE --body 'image'
```

> ⚠️ 必须配成**仓库级** Variable：`image` job 没有 `environment:`，读不到 Environment 级变量。

### Environment：`production`

- Required reviewers：部署前必须有人 Approve
- Deployment branches：建议限制为 `prod`
- ⚠️ 一旦限制了部署分支，用 `workflow_dispatch` 回滚时**必须在 UI 里把分支选成 `prod`**，
  否则 job 会被策略直接拒绝（表现为 2 秒内失败、且没有任何步骤日志，容易误判为代码问题）。
- ⚠️ GitHub 免费版账号的 **private** 仓库不支持分支保护与 Required reviewers
  （API 返回 403 / 422），需要把仓库设为 public 或升级 GitHub Pro。

### 分支保护

| 分支 | 规则 |
|---|---|
| `main` | 需要 PR；必过检查 `Test and build`；勾选 Require branches to be up to date；禁 force push / 禁删除 |
| `prod` | 需要 PR；必过检查 `Test and build` + `Guard prod source`；禁 force push / 禁删除 |

> 单账号无法给自己的 PR 审批，所以 `required_approving_review_count` 只能先设 0
> （仍然是「必须走 PR」）；加了协作者后再调到 1（main）/ 2（prod）。

> 注：`SSH_USER=deploy` 时 Actions 会把日志里的 "deploy" 打码成 `***`
> （`Trigger remote ***`、`/srv/mc` 显示成 `/srv/elicloud-***-test`），属正常行为。

## 配置与密钥放置表

| 变量 / 配置 | 位置 | 用途 |
|---|---|---|
| `SSH_HOST` / `SSH_USER` / `SSH_PORT` | GitHub Secrets | Actions 连哪台机器、以谁登录 |
| `SSH_KEY` | GitHub Secrets | 部署私钥（Actions 侧唯一凭据） |
| 对应公钥 | 服务器 `/home/deploy/.ssh/authorized_keys`（600） | 校验上面的私钥 |
| `GITHUB_TOKEN` | Actions 内置，无需配置 | 同仓库推 `ghcr.io` 镜像（靠 `packages: write`） |
| `GHCR_TOKEN` | GitHub Secrets（仅跨仓库/外部 registry） | 推镜像的替代凭据 |
| `DEPLOY_MODE` | GitHub Variables（**仓库级**） | `image` → 阶段一构建推镜像 |
| 应用运行时环境变量（`DATABASE_URL`、`LOG_LEVEL`…） | 服务器 `/srv/mc/app.env`（600，`deploy:deploy`） | 应用进程读取；不进仓库、不进 Actions |
| 应用运行时密钥（DB 口令、第三方 API Key） | 服务器 `/srv/mc/app.env` | 同上 |
| Compose 变量（`IMAGE_TAG`、`COMPOSE_PROJECT_NAME`…） | 服务器 `/srv/mc/app.env`（或 compose 同目录 `.env`） | `docker compose` 插值 |
| 部署 ref（`prod` / tag / commit SHA） | 由 Actions 作为参数传给 `deploy.sh` | 决定这次部署哪个版本 |
| 人工运维用私钥 | 本机 `~/.ssh/`（Windows：`C:\Users\<you>\.ssh\...`） | 仅供人登录服务器，与 Actions 无关 |

规则：**部署环节的凭据只走 GitHub Secrets；应用运行时的变量只在服务器 `app.env`。**
两边都不要写进仓库，也不要在 workflow 里 `echo` 出来。

### 本项目 `/srv/mc/app.env` 的真实变量清单

这些是**服务器上要填的值**（`.env.example` 是仓库里的模板，两者字段一致）：

| 变量 | 值 / 来源 | 说明 |
|---|---|---|
| `SSO_ISSUER` | `https://146.56.237.33/auth` | **必须与 SSO 的 `PUBLIC_BASE_URL` 逐字一致**，否则所有令牌验签失败 |
| `SSO_JWKS_URL` | `https://146.56.237.33/auth/.well-known/jwks.json` | 公钥集 |
| `JWT_AUDIENCE` | `elicloud-services` | access token 的 `aud`（`id_token` 的 `aud` 是 client_id，会被拒） |
| `REQUIRED_SCOPE` | `mc:whitelist` | 令牌必须含它，否则 403 `insufficient_scope` |
| `DATABASE_URL` | `sqlite:////data/mc.db` | 容器内路径；`/data` 由 compose 挂到项目目录 `data/` |
| `RCON_HOST` | `urania-mc` | 容器名；服务加入 `mc_default` 网络才解析得到 |
| `RCON_PORT` | `25575` | MC 的 RCON 端口（不发布到宿主机） |
| `RCON_PASSWORD` | 与 MC 容器 `RCON_PASSWORD` 一致 | **密钥**；取自 `docker inspect urania-mc` 的环境变量 |
| `RCON_TIMEOUT` | `5` | 秒 |
| `MAX_NAMES_PER_USER` | `2` | 每账号最多绑定的 MC 用户名 |
| `CORS_ORIGINS` | 前端来源（当前阶段填开发来源） | 逗号分隔 |
| `ADMIN_TOKEN` | 本服务自己的随机串（`openssl rand -hex 32`） | **与 SSO 的 `ADMIN_TOKEN` 是两把不同的令牌，不要复用**；留空 = `/v1/admin/*` 整组 403 |
| `SUBMIT_ATTEMPTS_PER_WINDOW` / `SUBMIT_WINDOW_SECONDS` | `10` / `600` | 提交与撤回的限流 |
| `LOG_LEVEL` | `info` | — |

## 服务器侧前置检查（有 SSH 就先做，能省大量返工）

下表右列是**本项目在 `146.56.237.33` 上实测的结果**（2026-10-06）：

| 检查 | 命令 | 本机实测结果 |
|---|---|---|
| root / sudo 可用 | `sudo -n true` | `docker-admin` **不可用**（需要密码）；`ubuntu` 可用（`(ALL) NOPASSWD: ALL`）→ **bootstrap 用 ubuntu 账号** |
| **Docker 是否 snap 装的** | `readlink -f "$(command -v docker)"` | **是**：`/snap/bin/docker -> /usr/bin/snap`。所以 **`deploy.sh` 里绝不能把 `/srv` 路径交给 docker**（`docker build /srv/…`、`--env-file /srv/…`、`-v /srv/…` 全失败）；变量要先在 shell 侧从 `app.env` 读出，再用 `-e` 传给容器 |
| docker 组 | `getent group docker` | `docker:x:1004:docker-admin,ubuntu,deploy` → `deploy` 已可免 sudo 用 docker |
| `deploy` 用户 | `id deploy` | 已存在：`uid=1008(deploy) groups=1008(deploy),100(users),1004(docker)` |
| 镜像仓库可达性 | `curl -s -o /dev/null -w '%{http_code}' https://ghcr.io/v2/` | 返回 `401`（0.6s，匿名探测的正常响应），但 **`docker pull ghcr.io/elicloudorg/sso:prod` 实测卡死 10 分钟无任何输出** → 见下方「ghcr 拉取风险」 |
| `/srv` 可用 | `ls -ld /srv` | 存在（`drwxr-xr-x root:root`） |
| 网关网络 | `docker network ls` | `dsh-nas_dsh-net`（网关反代）与 `mc_default`（RCON）都在；**两张都是 external** |

### 本项目的部署形态（与 SSO 同一套绕行）

```
本机 ──push──▶ GitHub ──Actions──▶ SSH(deploy@146.56.237.33)
                                      │
                                      ├─ scp deploy/deploy.sh → /srv/mc/deploy.sh
                                      └─ 执行 /srv/mc/deploy.sh prod
                                             │
                                             ├─ 读 /srv/mc/app.env（snap docker 看不到 /srv）
                                             ├─ 写 /home/deploy/elicloud-mc/.env（docker 可见）
                                             └─ docker compose up -d
                                                    ├─ 网络：dsh-nas_dsh-net + mc_default
                                                    ├─ 端口：仅 127.0.0.1:8001（8000 已被 sso 占）
                                                    └─ 数据：./data → /data（SQLite）
```

- **项目目录在 `/home/deploy/elicloud-mc`**（不是 `/srv`，也不是旧的
  `/home/docker-admin/elicloud/mc`）：snap docker 看不到 `/srv`，而 `/home/docker-admin`
  对 `deploy` 用户不可遍历，所以必须落在 `/home/deploy` 下。
- `/srv/mc/` 只放 `deploy.sh` 与 `app.env`。

### ghcr 拉取风险（本项目最重要的一处环境约束）

`docker pull ghcr.io/...` 在本机**实测会卡住**（有一次 `sso:prod` 拉了 10 分钟零输出）。
因此镜像模式下 `deploy.sh` 必须：

1. 用 `timeout` 给 `docker pull` 设上限（不要无限等）；
2. 拉取失败/超时时**回退到本地已有镜像**（`:prod` 或 `:sha-<short>`），而不是直接失败；
3. 在日志里明确打出"用的是拉取到的还是本地的镜像"，避免出现"部署成功但版本没变"。

可选缓解：给 dockerd 配 `registry-mirrors`（`/var/snap/docker/common/etc/docker/daemon.json`）。

### ghcr 的真实行为（实测三次，与直觉不同）

| 现象 | 真实含义 | 处理 |
|---|---|---|
| `docker pull ghcr.io/…` 返回 **`Error response from daemon: error from registry: denied`** | **该包还不存在**（首次部署、镜像 job 还没跑过），不是权限问题 —— 包是 public 的，匿名可拉（`ghcr.io/elicloudorg/sso:prod` 就一直在匿名拉） | 先让 `Build and push image` job 跑一次（push 到 `prod` 或 `workflow_dispatch`），包创建后即可拉 |
| pull 长时间停在 `Pulling fs layer`（实测数分钟级，`sso` 那次更久） | blob 下载慢，但**最终会成功** | `deploy.sh` 里的 `timeout 120 docker pull` 会在这时超时并回退本地镜像；**镜像已在本地时重跑一次即可切过去** |
| `curl -s -o /dev/null -w '%{http_code}' https://ghcr.io/v2/` 返回 `401` | 匿名探测的正常响应，**不代表不可达** | 不要据此判断"ghcr 挂了" |

> ⚠️ **部署顺序因此有依赖**：`DEPLOY_MODE=image` 时，**必须"先有镜像、后有部署"**。
> 首次部署（包还不存在）时，`deploy.sh` 会走 `denied` → 回退本地镜像 →
> **部署"成功"但线上版本没变**。这不是脚本的 bug，而是镜像模式的固有序：
> 框架的流水线本来就是「阶段一推镜像 → 阶段二部署」，只要走完整流水线就自然满足。

## 当前线上状态（2026-10-06 框架化完成）

| 项 | 值 |
|---|---|
| 容器 | `mc`（`/mc`），compose 项目 `mc`，`healthy` |
| 镜像 | **`ghcr.io/elicloudorg/mc:prod`**（切换前是手工构建的 `elicloud-mc:1.0.0`） |
| 项目目录 | `/home/deploy/elicloud-mc`（`compose` + `data/`，`data` 属主 `1002:1003` = 容器运行用户） |
| 运行时变量 | `/srv/mc/app.env`（600, `deploy:deploy`） |
| 数据 | SQLite 已从旧目录迁移：**33 条审计、3 条绑定历史全部保留** |
| 网络 | `dsh-nas_dsh-net` + `mc_default`；仅 `127.0.0.1:8001` 回环映射 |
| 网关 | `https://146.56.237.33/mc/healthz` → 200，`rcon.reachable=true` |
| 旧目录 | `/home/docker-admin/elicloud/mc`（手工部署遗留，容器已迁走；确认无用后可整目录删除，其中 `.env` 含旧的 `ADMIN_TOKEN`，**新值在 `/srv/mc/app.env`**） |

发布与回滚都走 `deploy-prod.yml`：合并到 `prod` 自动部署；回滚用
*Actions → Deploy to production → Run workflow*，`ref` 填旧的 `sha-<short>` 或提交 SHA
（镜像模式下阶段一会按该 ref 重新构建并推 `:prod`）。


## 日常发布流程

```bash
git switch main && git pull
git switch -c feature/xxx
# ... 开发 ...
git push -u origin feature/xxx        # 开 PR → main，等 CI 通过并合并
```
然后发 PR：`main` → `prod`。合并到 `prod` 即自动走 `deploy-prod.yml`：
先（可选）推镜像，再上传并执行 `/srv/mc/deploy.sh prod`。

## 回滚流程

两种方式，都只是把同一个 `deploy.sh` 用另一个 ref 再跑一次：

1. **GitHub Actions 回滚（推荐）**
   Actions → *Deploy to production* → *Run workflow* →
   `ref` 填上一个可用版本：`prod` 之前的 tag（如 `v1.3.0`）、commit SHA，或分支名。
   - ⚠️ 若 Environment 限制了 Deployment branches（建议 `prod`），UI 里的**分支必须选 `prod`**，
     否则 job 会被策略直接拒绝（2 秒失败、无步骤日志）。
   - 阶段一会按 `ref` 输入检出并构建**那个版本**（见 `deploy-prod.yml` 的
     `ref: ${{ github.event.inputs.ref || github.ref }}`），标签 `sha-<short>` 取自该 commit；
     阶段二仍然用当前分支的 `deploy.sh`。所以这条路径在 `DEPLOY_MODE=image` 下也是安全可回滚的。

2. **服务器上手工回滚**
   ```bash
   sudo -u deploy /srv/mc/deploy.sh <ref>
   ```
   适合 Actions 不可用时应急；注意这是绕过审批的路径，操作后请补记录。

镜像 tag 保留策略（`DEPLOY_MODE=image` 时）：每次部署推两个 tag —— `prod`（移动）
与 `sha-<short>`（不可变）。`sha-<short>` 是回滚锚点，请保留最近 N 个（建议 ≥10），
在镜像仓库里配置保留规则或定期清理；`prod` 永远指向当前线上版本。

## 常见问题排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| Actions 里 scp 一步失败 | `SSH_HOST`/`SSH_USER`/`SSH_PORT` 写错；私钥与服务器公钥不匹配；`/srv/mc` 不存在或不可写 | 用 `ssh -i deploy_key deploy@host` 复现；确认目录 `deploy:deploy 750` |
| `deploy.sh: Permission denied` | 服务器上文件没有可执行位 | workflow 已 `chmod +x`；手工上传时记得 `chmod +x` |
| `deploy.sh` 报 `bad interpreter: ...^M` | 上传/提交时带了 CRLF 换行 | `.gitattributes` 里加 `*.sh text eol=lf`，重新提交 |
| ssh-action 提示超时 | 构建时间长于 `command_timeout` | 调大 `command_timeout`，或把耗时构建放到阶段一 |
| 阶段一失败后阶段二没跑 | 这是设计行为：阶段一失败则部署中止 | 修好构建再重跑 |
| 阶段二显示 skipped | `DEPLOY_MODE` 既不是 `image`，但同时阶段一被跳过时不应 skip | 检查 `deploy` job 的 `if` 条件是否被改动 |
| 部署成功但应用没更新 | `deploy.sh` 里没有真正的重启/切换逻辑（骨架默认什么都不做） | 补齐 `deploy.sh` 的项目部署逻辑 |
| Actions 拿不到运行时变量 | 运行时变量属于服务器 `app.env`，不在 Actions 里 | 在服务器上编辑 `/srv/mc/app.env` |

## 相关文件与配置位置

| 文件 / 配置 | 位置 | 说明 |
|---|---|---|
| CI 工作流 | `.github/workflows/ci.yml` | PR 到 main/prod、push 到 main |
| 部署工作流 | `.github/workflows/deploy-prod.yml` | push 到 prod、workflow_dispatch |
| 部署脚本（版本管理） | `deploy/deploy.sh` | 上传到 `/srv/mc/deploy.sh` 执行 |
| 服务器初始化脚本 | `scripts/bootstrap-server.sh` | 在服务器上以 root 跑一次 |
| 部署密钥与服务器信息 | GitHub Secrets | `SSH_HOST`/`SSH_USER`/`SSH_KEY`/`SSH_PORT` |
| 部署模式开关 | GitHub Variables | `DEPLOY_MODE=image` 才构建推镜像 |
| 运行时环境变量 | 服务器 `/srv/mc/app.env` | 600，`deploy:deploy`，不进仓库 |
