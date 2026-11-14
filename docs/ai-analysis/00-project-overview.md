# 00: Project Overview — chatgpt2api

Status: current（基于 2026-11-13 只读评审；事实以来源路径为准）

## 项目概览

ChatGPT2API 是一个自托管网关：通过逆向 ChatGPT Web 协议，把一组"上游账号"
（Upstream Account）池化，对外提供 OpenAI 兼容的文本、搜索、图片与可编辑文件
接口，并附带一个 Vue 3 控制台用于观测和管理这些调用（`CONTEXT.md`）。

核心功能：

- OpenAI 兼容 API：`/v1/chat/completions`、`/v1/responses`、`/v1/messages`
  （Anthropic 风格）、`/v1/search`、`/v1/images/generations`、`/v1/images/edits`、
  `/v1/models`、`/v1/editable-file-tasks`（PPT/PSD）、`/v1/ppt|psd/generations`
  （`api/ai.py`）
- 多上游账号池：手动/OAuth 导入、CPA 池与 Sub2API 服务器批量导入、AT 自动刷新、
  分组、批量维护（`api/accounts.py`、`services/account_service.py`）
- 本地 User Key 鉴权 + admin/studio 能力模型（`api/support.py`、
  `services/auth_service.py`）
- 控制台：Dashboard、Accounts、Settings、Proxy、Logs、Monitor、Gallery、Studio
  （`web-vue/src/router/routes.ts`）
- 运维：备份（本地/R2）、在线更新（GitHub Release）、日志保留清理、图片存储
  （本地/WebDAV）、代理池、Prompt Library、Dashboard 指标投影

目标用户：需要把 ChatGPT 网页版能力包装成 API 供下游应用消费的个人/小团队
自托管者。README 明确警告上游调用违反 OpenAI ToS、账号可能被封。

## 技术栈

| 层 | 技术 | 版本/来源 |
| --- | --- | --- |
| 后端 | Python + FastAPI + Uvicorn | `requires-python >=3.13`，`pyproject.toml` |
| 上游 HTTP | curl-cffi（Chrome 指纹 impersonate） | `curl-cffi>=0.15.0` |
| ORM/DB | SQLAlchemy 2；SQLite（默认）或 PostgreSQL（psycopg2-binary） | `services/application_database.py`、`services/storage/` |
| 图片处理 | Pillow；可选 Node/Sharp 放大 | `pillow>=12.2.0`、`scripts/image_upscale/` |
| 前端 | Vue 3 + Vite + TypeScript + Pinia + Vue Router + Tailwind + nanocat-ui | `web-vue/package.json` |
| 部署 | Docker 多阶段构建（node:22 构建前端 → python:3.13-slim 运行） | `Dockerfile`、`docker-compose*.yml` |
| 协议适配 | `services/protocol/`：OpenAI chat/responses/search/image、Anthropic messages | 自研适配层 |
| 反爬对抗 | `utils/pow.py`、`utils/turnstile.py`、`utils/pkce.py` | 自研 |

入口：`main.py` → `api.app:create_app()`；`__main__` 分支直接
`uvicorn.run(app)`（`main.py`）。

## 目录结构

```
├── main.py             # 入口：create_app + uvicorn
├── api/                # HTTP 层：app.py 装配 5 个 router + SPA 静态兜底
│   ├── ai.py           # OpenAI/Anthropic 兼容业务接口（require_identity）
│   ├── accounts.py     # 管理接口：User Key、账号、OAuth、CPA、Sub2API（require_admin）
│   ├── system.py       # 登录/版本/更新/设置/图库/日志/监控/代理/Dashboard/备份
│   ├── image_tasks.py  # Studio 异步图片任务（require_identity）
│   ├── prompts.py      # Prompt Library 读取与 Prompt Source 管理
│   ├── support.py      # 鉴权依赖、SPA 资源解析、账号生命周期 watcher
│   ├── image_inputs.py # 图片输入下载/校验（SSRF 防护集中在此）
│   └── *_contract.py   # 传输层校验模型
├── contracts/          # Pydantic 稳定契约：auth/models/settings/proxy/prompts/updates
├── services/           # 领域服务（约 3.3 万行，~56 个模块）
│   ├── storage/        # Application Database：SQLAlchemy 模型与各域 Repository
│   ├── protocol/       # 上游协议适配与响应封装
│   └── *_view.py       # 后端投影构建器（服务 → 稳定 JSON）
├── utils/              # pkce/pow/turnstile（上游反爬）、diagnostics（脱敏）、log 等
├── web-vue/            # 控制台：views/ + api/ 适配器 + components/{ai,studio,ui}
├── scripts/image_upscale/  # 可选 Node/Sharp 放大运行时
├── deploy/             # install.sh 交互式安装、docker-entrypoint.sh 受管运行目录
├── docs/               # 架构地图 maps/、ADR、需求 requirements/、runbooks、references
├── Dockerfile / docker-compose*.yml
└── .github/workflows/docker-publish.yml
```

## 模块职责与核心调用关系

### 请求主链路

```
HTTP → api/{ai,accounts,image_tasks,prompts,system}.py
        ├─ api/support.py: require_identity / require_admin（Bearer）
        ├─ *_contract.py: Pydantic 传输校验
        └─ services/<domain>_service.py（唯一业务所有者）
              ├─ services/storage/<domain>_repository.py → Application Database
              ├─ services/protocol/* → OpenAIBackendAPI → curl-cffi → ChatGPT Web
              └─ services/*_view.py → 投影 → contracts 响应模型 → web-vue api 适配器
```

投影边界（`docs/maps/backend-map.md`）：服务只交投影，Vue 不重算业务语义；
命令结果要么已完成、要么是显式异步任务（polling 由对应 owner 定义）。

### 关键服务-存储对应（`docs/maps/backend-map.md`）

| 领域 | 服务 | 持久化 |
| --- | --- | --- |
| 上游账号/User Key | `AccountService`、`AuthService` | Account Repository（`database_storage.py`） |
| 设置 | `SettingsManagementService` | `SystemSettingsRepository`（revision CAS） |
| 代理 | `ProxyManagementService`、`ProxyService` | `ProxyConfigurationRepository` |
| 调用记录 | `CallRecordService`（`log_service.py` 别名） | `CallRecordRepository` |
| 实时监控 | `RealtimeMonitorService` | 仅进程内存 |
| Dashboard | `DashboardMetricsService` | `DashboardMetricsRepository`（state/hourly/model_hourly 三表） |
| 图片任务 | `ImageTaskService` | `data/image_tasks.json` |
| 图片资产/图库 | `ImageStorageService` | 本地或 WebDAV + `data/image_index.json` |
| 可编辑文件任务 | `EditableFileTaskService` | `EditableFileTaskRepository` + 文件系统 |
| Prompt Library | `PromptLibraryService` | `PromptLibraryRepository` |
| 远程导入 | `CPAImportService`/`Sub2APIImportService`/`RemoteImportJobCoordinator` | `remote_import_configuration_repository` + Account Repository |
| 在线更新 | `UpdateService`/`UpdateStatusService` | Release 元数据 + `data/update_task.json` + 受管 `/app` 目录 |
| 备份 | `BackupService` | pg_dump/SQLite 文件 + 可选 R2（openssl 加密） |

### 并发与共享基础设施

- `CHATGPT2API_THREAD_TOKENS`（默认 120）统一给 anyio 默认 thread limiter 设容
  量，并写入 `RealtimeMonitorService`（`api/app.py` `_configure_threadpool`）。
- `bounded_task_runner.py` 与 `account_processing.py` 共享"账号处理并发"槽位：
  远程维护每账号 1 槽，本地原子批量整体 1 槽（`CONTEXT.md` 定义）。
- `image_task_service` 与 GenBox 推送有各自有界 worker/队列；图片抓取并发 8。
- 生命周期：`create_app` lifespan 启动 GenBox push、ImageTaskService、保留清理
  调度、Dashboard 投影重置/同步/刷新、账号 watcher、BackupService；退出时按序
  join/shutdown（`api/app.py:52-113`）。

## 启动方式

| 模式 | 命令 | 说明 |
| --- | --- | --- |
| Docker（推荐） | `cp .env.example .env`（填 `CHATGPT2API_AUTH_KEY`）→ `docker compose up -d` | 宿主 3000 → 容器 80；SQLite 默认 |
| Docker + PG | `docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d` | postgres:18-alpine overlay |
| 本地构建镜像 | `docker compose -f docker-compose.local.yml up -d --build` | 源码构建 |
| Python 源码 | `uv sync` → `uv run uvicorn main:app --host 0.0.0.0 --port 3000` | `deploy/install.sh` 的 `run_python` 路径；需先有 `web_dist` |
| 一键安装 | `bash deploy/install.sh` 或 curl 管道 | 交互式：模式/端口/并发/目录/分支/数据库/auth key |

运行时工作目录为 `/app`：`docker-entrypoint.sh` 在镜像版本变化时把
`/opt/chatgpt2api` 种子目录拷入 `/app`（保留 `data/`、`config.json`、`.venv`、
版本标记），再 `uv sync --frozen` 后 `exec uvicorn`（`deploy/docker-entrypoint.sh`）。

## 构建方式

- 后端：无编译产物；CI 用 `uv run python -m compileall -q api contracts services utils main.py` 做语法检查。
- 前端：`cd web-vue && npm ci && npm run build`（`icons:check` → `tsc` → `vite build` → `dist/`；源码模式需手动拷到 `web_dist/`，`deploy/install.sh:658-669`）。
- Docker：`docker buildx build`（amd64+arm64，`docker-publish.yml`）。

## 测试方式

- 后端：`pytest`（`pyproject.toml` 配置 `testpaths=["tests"]`、dev 依赖含 `pytest`+`httpx`）。**当前工作区 `tests/` 不存在**——`.gitignore` 排除了测试目录。
- 前端：`npm run test:runtime` 引用 `web-vue/tests/*.mjs`（23 个运行时契约测试），**该目录同样 gitignore 且本地不存在**。
- CI：`docker-publish.yml` 的 `verify` job 只做 `compileall` + `npm run build`，不执行 pytest/前端测试。即：**安全敏感路径当前无自动化回归网**。

## 关键依赖

后端（`pyproject.toml` + `uv.lock` 锁定）：fastapi、uvicorn、sqlalchemy、
psycopg2-binary、curl-cffi、pillow、tiktoken、python-multipart、pybase64。

前端（`web-vue/package-lock.json` 锁定）：vue、vue-router、pinia、axios、
markdown-it、highlight.js、js-beautify、vue-virtual-scroller、nanocat-ui
（自有组件库，`@iconify/vue` 图标）。

关键环境变量：必填 `CHATGPT2API_AUTH_KEY`；可选 `CHATGPT2API_PORT`（3000）、
`CHATGPT2API_THREAD_TOKENS`（120）、`CHATGPT2API_BASE_URL`、`DATABASE_URL`、
`POSTGRES_*`、`TZ`、`CHATGPT2API_IMAGE`、`CHATGPT2API_BUILD_TYPE`（Dockerfile）。

## 已知问题（与 `TODO.md` 对应）

安全评审发现的问题已登记在根目录 `TODO.md`，摘要：

- P0：OAuth code→token 兑换 `verify=False`（`services/oauth_login_service.py`）
- P1：账号凭据明文落库、日志脱敏双轨（`utils/log.py` 覆盖面窄）、WebDAV
  与全局代理/TLS 体系脱节、认证无失败限流
- P2：无内置 TLS、容器 root 运行、CORS `*`、app 无 healthcheck/资源限制、
  auth 快照多副本窗口
- P3：SVG 内联、无界列表、`download_images_zip` 内存、Sub2API scheme、
  非恒定时间比较、`/docs` 暴露、OAuth 调试 print
- P4：版本漂移（`VERSION`=3.2.9 vs README v3.2.3）、测试不入库且 CI 无 pytest

## 待确认事项

1. **OAuth `verify=False` 是否故意为之**——需向作者确认或修复（`TODO.md` P0）。
2. **`docker-compose.yml` 挂载 `./config.json`**：本地无该文件时 Docker 会把它
   创建为目录；`deploy/install.sh` 会预写 config.json，但手工 compose 路径
   依赖用户先建文件——是否为已知取舍待确认。
3. **测试为何不入库**：`tests/`、`web-vue/tests/` 被 gitignore 是策略还是遗留，
   待确认；这决定 P4 修复方式（入库 vs 私有测试流程）。
4. **`config.json` 与 `DATABASE_URL`/settings 表的优先级边界**：`services/config.py`
   混合读取文件与库内 settings，确切合并语义需要逐字段确认（本次未逐行核对）。
5. **`web_dist` 来源**：仓库不含 `web_dist/`；源码模式必须先前端构建。install.sh
   有 fallback（无 npm 时沿用旧 `web_dist`），该路径在干净环境是否可行未验证。
6. **多副本部署假设**：auth 快照、monitor、image task 均为进程内状态，项目隐含
   单实例假设；是否支持多副本部署未经官方文档确认。

## 本地运行可行性（本机环境实测）

| 条件 | 状态 |
| --- | --- |
| uv 0.8.19 | 可用；`uv python list` 显示 cpython-3.13.7 可自动下载 |
| 系统 python3 | 3.9.6，不满足 `>=3.13`；须走 uv 管理的 Python |
| node | v22.18.0，满足 node:22 构建要求 |
| docker | 29.9.2 + compose 可用 |
| 结论 | 三条路径均可运行；最省事是 `docker compose up -d`；源码模式 `uv sync` 会自动拉取 3.13 |

## 未完成模块 / 遗留迹象

- 代码中无 `TODO`/`FIXME`/`XXX`/`HACK` 标记（全仓 grep 仅 LICENSE 命中）。
- 调试残留：`api/accounts.py` OAuth finish 与 `oauth_login_service.py` 的失败
  分支向 stdout `print`（已列入 `TODO.md` P3-17）。
- `pyproject.toml` 的 `description` 仍是占位文本 "Add your description here"。
- `docs/requirements/` 有两个未完结 PRD：0001（implementing）、
  0002（pending-verification）。
