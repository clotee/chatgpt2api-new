# 部署与升级

状态：当前

本项目的发布镜像默认是 `ghcr.io/yukkcat/chatgpt2api:latest`。标准 Compose 将服务暴露在 `3000` 端口，使用 `chatgpt2api-runtime` 命名卷保存可更新的应用运行目录，并单独挂载本地 `data/` 和 `config.json`。运行时配置和数据不应提交到 Git。

## Docker 部署

```bash
git clone https://github.com/yukkcat/chatgpt2api.git
cd chatgpt2api
cp .env.example .env
# 将 .env 中的 CHATGPT2API_AUTH_KEY=your_secret_key_here 替换为私有密钥。
test -f config.json || printf '{}\n' > config.json
docker compose up -d
```

镜像中的 `/opt/chatgpt2api` 是只读应用种子，`/app` 是受管运行目录。首次启动或镜像版本变化时，入口脚本会用镜像种子刷新 `/app`，再按锁文件同步 Python 依赖；同一镜像正常重启时会保留控制台在线更新后的运行版本。业务数据始终留在独立的 `data/` 挂载中。

### 本地 PostgreSQL 18

需要由 Compose 一并运行 PostgreSQL 时，在 `.env` 中设置数据库密码：

```dotenv
POSTGRES_PASSWORD=replace_with_a_strong_password
```

然后同时加载基础 Compose 和 PostgreSQL overlay：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

`docker-compose.postgres.yml` 使用官方 `postgres:18-alpine` 镜像，等待数据库健康后再启动应用，并将数据持久化到 `chatgpt2api-postgres-data` 命名卷。数据库端口默认不暴露到宿主机。

`POSTGRES_PASSWORD` 会同时用于初始化数据库和构造 `DATABASE_URL`，因此请仅使用 URL 安全字符（字母、数字、下划线或连字符），不要在两个位置分别编码密码。
启用该模式后，后续启动、升级、查看状态和停止服务都应同时指定这两个 Compose 文件。

查看状态与日志：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml ps
docker compose -f docker-compose.yml -f docker-compose.postgres.yml logs -f postgres app
```

`.env` 中的 `CHATGPT2API_AUTH_KEY` 优先于 `config.json` 的 `auth-key`。若要使用 `config.json`，先删除或注释 `.env` 中的该值，再填写 `auth-key`。

`./config.json` 是绑定挂载文件：Docker 只会把宿主机上**已存在的普通文件**挂载进容器；如果该路径不存在，Docker 会创建一个同名**目录**，应用将无法读取配置。首次部署请先执行 `test -f config.json || printf '{}\n' > config.json`（上面的快速部署命令已包含该步骤）。

默认地址：

- 控制台：`http://localhost:3000`
- API：`http://localhost:3000/v1`

也可以使用仓库安装脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/yukkcat/chatgpt2api/main/deploy/install.sh | sudo bash
```

安装脚本会明确询问 Application Database：

- SQLite 本地文件（默认）
- PostgreSQL 18 本地容器（Docker 模式）
- 已有 PostgreSQL URL

选择本地 PostgreSQL 时，脚本会自动生成并保存数据库密码，下载 `docker-compose.postgres.yml`，再与主 Compose 一起启动。重复运行安装脚本会复用已有密码。`CHATGPT2API_THREAD_TOKENS` 默认是 `120`，表示后端同步工作线程的并发容量，只要求正整数且不设置人为最高值；账号、代理和上游服务仍分别执行自己的并发限制。

安装脚本默认跟随 `main` 分支。`BRANCH`（环境变量或 `--branch` 参数）同时决定克隆的 Git 分支/标签和默认镜像 tag；正式部署应固定为发布 tag，例如：

```bash
BRANCH=v3.2.9 curl -fsSL https://raw.githubusercontent.com/yukkcat/chatgpt2api/v3.2.9/deploy/install.sh | sudo bash -s -- --branch v3.2.9
```

### 反向代理与 HTTPS

应用默认以 HTTP 提供服务（容器内监听 `80` 端口）。**公网部署必须**在前面加一层支持 TLS 的反向代理，不要把 `3000` 端口直接暴露到公网；仅限内网或本机访问的部署可以不启用 HTTPS。

Caddy（自动申请和续期证书）：

```caddyfile
chatgpt2api.example.com {
    reverse_proxy 127.0.0.1:3000
}
```

Nginx（证书由 certbot / ACME 等签发）：

```nginx
server {
    listen 443 ssl;
    server_name chatgpt2api.example.com;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

TLS 在反向代理层终止，客户端仍以 `Authorization: Bearer` 头传递密钥，应用本身不需要额外配置。公网域名确定后，把 `CHATGPT2API_BASE_URL` 设为 `https://chatgpt2api.example.com`，保证生成的图片和文件链接使用 HTTPS。

## 本地开发

后端：

```bash
git clone https://github.com/yukkcat/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

Vue 控制台：

```bash
cd web-vue
npm install
npm run dev
```

前端开发服务器默认使用 Vite 端口；后端仍读取项目根目录的 `config.json` 和 `data/`。

## 存储边界

`DATABASE_URL` 选择 Application Database；未设置时使用
`data/chatgpt2api.db`。支持 SQLite 与 PostgreSQL 18，不再通过
`STORAGE_BACKEND` 选择 JSON、Git 或账号专用数据库。

选择数据库不是旧数据迁移操作，不会自动导入 JSON、JSONL、Git 或旧账号
SQLite 文件。图片文件及其相关索引仍按图片存储边界管理。完整边界见
[`storage-architecture.md`](storage-architecture.md)。

## 安全边界

### 管理员密钥的信任级别

`CHATGPT2API_AUTH_KEY`（或 `config.json` 的 `auth-key`）是管理员密钥：持有它等价于完全控制实例——可以管理上游账号与凭据、修改全部设置、签发和吊销用户密钥、触发在线更新与备份。请把它当作与上游账号凭据同级的秘密保管：只放在环境变量或密钥管理系统中，不写入仓库、日志或工单。

### 密钥吊销生效窗口与单实例假设

- 用户密钥（控制台设置中的用户密钥管理，`/api/auth/users`）在认证快照中缓存：快照约每 5 秒尝试刷新，最长可保留约 30 秒旧状态。因此禁用或删除用户密钥后，旧密钥最多还可继续使用约 30 秒；同一进程内的修改会立即更新本地缓存。
- 认证快照、实时监控、图片任务、账号生命周期 watcher 等运行时状态都保存在单个进程内。**本项目不支持多实例部署**：不要在负载均衡后运行多个副本，否则运行时状态不会收敛。需要保障可用性时使用单实例加进程守护（Compose `restart: unless-stopped` 或 systemd）。

### 公开下载端点依赖不可猜测的 URL

`/images/...`、`/image-thumbnails/...` 和 `/files/...` 下载端点不需要认证：任何拿到链接的人都可以下载对应文件。可编辑文件（PPT / PSD）使用随机 UUID 存储路径；图片使用「日期目录 + 时间戳 + 内容哈希」路径。请把这些链接视为 bearer 秘密——不要公开发布、不要写进公开日志；如需收敛访问范围，在反向代理层增加访问控制。

### 密钥轮换流程

- 管理员密钥：修改 `CHATGPT2API_AUTH_KEY` 环境变量并重启服务（环境变量在进程启动时读取）；或修改 `config.json` 的 `auth-key`（约 2 秒内生效，无需重启）。轮换后旧密钥立即失效，需同步更新所有管理员客户端。
- 用户密钥：在控制台用户密钥管理中新建密钥并分发给客户端，确认切换完成后禁用或删除旧密钥；吊销在约 30 秒内生效。也可以通过更新接口直接替换某条密钥的值。

## 升级

升级前先在系统设置中执行一次 R2 备份，并确认状态为成功。备份归档始终包含
Application Database：SQLite 使用 `data/application-database.sqlite3`，PostgreSQL
使用 `data/application-database.pgdump`。图片任务记录、PPT / PSD 文件和图片目录
按备份设置选择；外部 WebDAV 仍需独立备份。

未配置 R2 时，应先停止服务再备份。SQLite 可以在停服后复制 `data/chatgpt2api.db`；
PostgreSQL 必须使用 `pg_dump --format=custom`，不能用 `tar data/` 代替数据库备份。
`config.json` 只保留 `auth-key` 等启动配置，也应单独保存：

```bash
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

本地 PostgreSQL Compose 可直接在数据库容器内导出：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec -T postgres \
  sh -c 'pg_dump --format=custom --no-owner --no-privileges \
  -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

### 控制台在线更新

使用当前标准 Compose 部署时，版本弹窗可直接启动在线更新。后端负责下载发布包、校验 SHA-256 与更新清单、替换运行文件、同步依赖和安排容器重启；前端只展示后端任务，刷新页面或重启完成后仍可恢复任务结果。文件或依赖同步失败时会恢复旧文件，并按旧锁文件重新同步依赖。

源码运行和没有挂载受管 `/app` 运行目录的旧容器不会显示“立即更新”，只会给出 Git 或镜像升级提示。首次切换到受管运行目录时，应先更新仓库中的 Compose 文件，再执行一次镜像升级：

```bash
git pull --ff-only
docker compose pull
docker compose up -d
```

不要把 `/app` 运行时卷当作业务备份；它可以从发布镜像重新创建。不要使用 `docker compose down -v` 执行普通升级，因为该命令还会删除 Compose 管理的命名卷。

### 命令行升级

镜像部署升级：

```bash
docker compose pull
docker compose up -d
```

本地 PostgreSQL Compose 部署升级：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml pull
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

镜像部署固定或回退版本时，在 `.env` 设置 `CHATGPT2API_IMAGE=ghcr.io/yukkcat/chatgpt2api:<tag>`，再执行对应的 `pull` 与 `up`。镜像版本变化后，入口脚本会用该镜像刷新受管运行目录；Git 检出标签只影响源码运行，不会改变 Compose 使用的镜像版本。升级后检查：

```bash
docker compose ps
docker logs -f chatgpt2api
```

## 回滚与维护

先停止对应 Compose，再恢复经过验证的代码 / 镜像和备份数据；不要在运行时直接覆盖 `data/`。常用命令：

```bash
docker compose restart
docker compose down
```

`docker image prune` 只清理未使用镜像，不会替代数据备份。
