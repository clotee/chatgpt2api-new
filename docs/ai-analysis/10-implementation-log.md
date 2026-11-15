# 10: Implementation Log — chatgpt2api

Status: current（实施记录，随任务推进更新）

记录已批准计划的实际执行情况：完成内容、修改文件、技术决策、
测试证据、未解决问题、回滚方式。对应计划：`06`/`07`/`08`，
评审修正：`09`。

---

## Phase 1 — P0 安全止血（已完成，待用户验收）

执行日期：2026-11-13。范围：T-01 ~ T-05 全部子项。
**未提交 git**（按规则等待用户授权）。

### T-01 OAuth 换 token 启用 TLS 校验

- **修改**：`services/oauth_login_service.py:196` `verify=False`→`verify=True`
- **技术决策**：`build_session_kwargs` 中用户显式配置的
  `skip_ssl_verify` 仍可覆盖为 False——保留用户 opt-out 能力
  （该设置属管理员显式风险开关），消除的是**无条件**禁用。
- **验证**：`proxy_settings.build_session_kwargs(impersonate="chrome",
  verify=True)` 实测返回 `verify: True`；全仓 `verify=False` grep 0 命中。
- **未验证**：真实 OAuth 导入全流程（需真实账号，L4 手工项）。
- **回滚**：单行 revert。

### T-02 依赖升级批次 A

- **修改**：`uv.lock`（`uv lock --upgrade-package`）
  - starlette 1.0.0 → **1.10.0**（目标 ≥1.3.1，uv 解析到最新；OSV 复查 0 漏洞）
  - python-multipart 0.0.26 → **0.0.33**（≥0.0.31，覆盖评审修正的最新修复版）
  - anyio 4.13.0 → **4.14.2**
  - 无传递依赖连带变更（diff 仅 3 包）
- **兼容性验证**：
  - `compileall` 全量通过；`import main` 116 路由注册成功
  - uvicorn 实测：`/version` 200、`/auth/status` 200、`/v1/models`
    无认证 401、`/api/accounts` 无认证 401
  - multipart `POST /v1/images/edits` → 正常 JSON 4xx（非 500）
  - urlencoded → JSON 端点 → 正常 422 校验错误
  - `/v1/chat/completions` 走到上游调用阶段（本环境无上游账号，
    连接超时属预期，非回归）
- **决策记录**：fastapi 0.136.0 `requires_dist` 为
  `starlette>=0.46.0` 无上限（PyPI 实测），1.10.0 版本约束兼容；
  fastapi↔starlette 行为兼容经上述冒烟验证。
- **回滚**：`git checkout uv.lock` + `uv sync`。

### T-03 Pillow 13.x

- **修改**：`uv.lock` pillow 12.2.0→13.0.0；`pyproject.toml`
  下限 `>=12.2.0`→`>=13.0.0`（锁定安全下限）
- **API 兼容核查**：项目使用 `Image.open`×12、`ImageOps.
  exif_transpose`×4、`Image.Resampling`×2、`Image.MIME`×2、
  `Image.new`×1、`Image.LANCZOS`×1（`openai_v1_image_edit.py:47`）
- **验证**：实测 PNG/JPEG/WEBP/GIF 解码、exif_transpose、
  thumbnail+Resampling.LANCZOS、`Image.LANCZOS` 别名（13.0.0 仍
  有效）、`Image.MIME` 映射、`convert/resize/split` 全部正常
- **OSV 复查**：pillow 13.0.0 **0 漏洞**（12.2.0 的 88 项全部修复）
- **未验证**：真实上游图片全链路（需账号）；EXIF 旋转实图抽查
- **回滚**：`git checkout uv.lock pyproject.toml` + `uv sync`。

### T-04 前端依赖升级

- **修改**：`web-vue/package-lock.json`（package.json 范围未变，
  caret 已覆盖）
  - axios 1.19.0 → 1.21.1；follow-redirects 1.16.0 → 1.16.3（传递）
  - markdown-it 14.2.0 → 14.3.3（**未升 15.x**，守 major 边界）
- **验证**：`npm run build`（icons:check + tsc + vite）通过；
  OSV 复查 axios 1.21.1 / markdown-it 14.3.3 均 0 漏洞
- **备注**：`npm audit` 不可用（npmmirror registry 不支持 audit
  端点），以 OSV.dev 逐版本核查替代。
- **回滚**：`git checkout web-vue/package-lock.json && npm ci`。

### T-05 琐碎安全包（6 子项全做）

| 子项 | 修改 | 验证 |
| --- | --- | --- |
| compare_digest | `api/support.py` `token == auth_key` → `hmac.compare_digest`（utf-8 encode 防非 ASCII TypeError） | 实测正确 key 200 / 错误 key 401 |
| docs 开关 | `api/app.py` 新增 `CHATGPT2API_ENABLE_DOCS` env（默认关）；`docs_url/redoc_url/openapi_url` 条件注入 | 实测 `/docs` `/redoc` `/openapi.json` 均 404 |
| max_items | `api/accounts.py` 20 处请求模型 `max_length=5000`；`api/system.py` paths/ids 5000、tags 200（保持 required 语义） | 实测 5001 项 → 422 `too_long`；tags 缺失仍 422、超限仍 422 |
| SVG 头 | `services/image_service.py` 3 处 svg Response 加 `Content-Security-Policy: sandbox`（get_image_response 两分支 + get_thumbnail_response；download 端点本就有 attachment） | 代码审查确认；<img> 嵌入不受影响 |
| base_url 校验 | `api/accounts.py` 新增 `_validate_remote_base_url`（scheme∈{http,https}+netloc），应用于 CPA/Sub2API create+update 4 端点 | 实测 `javascript:`/`ftp:` → 400；`https://`/`http://内网` 正常创建 |
| print→logger | `oauth_login_service.py` + `api/accounts.py` 共 3 处 print → `utils.log.logger`（warning/info），raw_body 过 `sanitize_diagnostic_text` | compileall + 导入冒烟 |

**技术决策记录**（对应评审 D9/D10，用户未单独指定，取非破坏性默认）：
- docs：env opt-in 默认关（安全默认；需要时 `CHATGPT2API_ENABLE_DOCS=1`）
- base_url：允许 http/https，拒绝其他 scheme；http 明文凭据风险
  留待 T-29 文档化（D10 未决，未阻断合法内网部署）

**遗留 print（不属本阶段，留 T-16）**：`api/support.py:85-104`
account-watcher 4 处、`services/json_file.py`（底层库 stderr，计划
保留）、`services/storage/factory.py:13` 启动信息（有意 console 输出）、
`conversation.py:1154` 注释掉的调试行。

### Phase 1 验证汇总

| 检查 | 结果 |
| --- | --- |
| `compileall api contracts services utils main.py` | ✅ 通过 |
| `import main` 路由注册 | ✅ 116 路由 |
| uvicorn 冒烟（version/auth/401/docs 404/校验 422/400） | ✅ 全部符合预期 |
| OSV 复查 starlette 1.10.0 / python-multipart 0.0.33 / anyio 4.14.2 / pillow 13.0.0 | ✅ 各 0 漏洞 |
| OSV 复查 axios 1.21.1 / markdown-it 14.3.3 | ✅ 各 0 漏洞 |
| `npm run build`（tsc+vite） | ✅ 通过 |
| Pillow API 实测（4 格式+常用 API） | ✅ 通过 |
| `verify=False` 残留 grep | ✅ 0 命中 |
| git diff 审查 | ✅ 仅预期文件，无密钥/无无关改动 |

**修改文件清单**：`services/oauth_login_service.py`、
`services/image_service.py`、`api/app.py`、`api/support.py`、
`api/system.py`、`api/accounts.py`、`pyproject.toml`、`uv.lock`、
`web-vue/package-lock.json`（共 9 个，+166/−123 行）。

**本地环境副产物**（gitignored，不影响仓库）：`data/`、`.venv/`、
`web-vue/dist/`、`/tmp` 测试文件；测试用 Sub2API server 已删除。

### Phase 1 遗留与下一步

- Phase 1 验收矩阵其余项（真实 OAuth 导入、真实上游图片链路）
  需真实账号环境手工验证——已在上表标注"未验证"。
- **下一步按计划进入 Phase 2 前须注意**：评审 `09` 已证伪
  PERF-01/PERF-03 前提（T-09/T-10 需按评审修正重做），且 D2
  测试策略应前置为 Phase 4 门禁。Phase 2 中 T-06/T-07/T-08/
  T-11/T-12 证据仍成立可执行；T-09/T-10 按评审结论修正或暂停。

---

## Phase 2 · T-09 修订版实施记录（评审修正后）

**背景**：评审 `09` 证伪原 T-09/T-10 前提。本轮经独立复核+实测
确认：T-10 关闭（wontfix），T-09 按修订方案实施——放弃原
"写路径建索引+对账"方案，改为 `image_storage_service.py` 单文件
内增量优化。

### 复核结论（实测，非估算）

- 图库热路径（分页）真实成本 @5000 项 index：原 ~143ms，
  分解为 `_load_clean_index` ~54ms + `_public_url` 按项重算 ~40ms
  + `_is_image_rel` 与 clean 过滤重复校验 ~30ms + 其余 ~19ms。
- 账号链路 @1000：dict 拷贝 0.1ms + 过滤 0.2-1ms + 投影 0.3ms
  ≈1-2ms/请求 → **T-10 证伪成立，wontfix**。

### 实施内容（T-09a/b/c，单文件 +54/−24 行）

| 子项 | 改动 | 文件位置 |
| --- | --- | --- |
| T-09a | `_is_image_rel` 增加廉价扩展名预检（先于 pathlib normalize），消除每项 ~30µs 重路径；`.strip()` 与 normalize 语义对齐 | `image_storage_service.py:147` |
| T-09b | `_load_clean_index` 增加 `(mtime_ns, size)` 键进程内缓存；读路径共享只读 dict，9 处写路径改 `_load_clean_index_mutable()`（deepcopy），`list_items` 用惰性 `mutable_indexed()` 仅在确需变更时拷贝 | `:316-344` 及 9 处调用点 |
| T-09c | `_public_url` 拆出循环不变量 `_public_url_prefix`，`list_items` 每请求计算一次 URL 前缀替代每项重算 settings+normalize | `:452-460`、`:767,873` |

**T-09d（verify_existing TTL 节流）暂缓**：非分页/维护路径，
节流会改变"缺失文件即剔除"语义，收益面小，记录为候选后续项。

### 正确性验证（`/tmp/verify_t09.py`，合成 5000 项索引）

- `_is_image_rel` 新旧语义等价：22/22 边界用例一致（含尾随空格、
  反斜杠、dotfile、unicode、畸形路径）——首轮发现 `.strip()` 遗漏
  已修复复测通过。
- 缓存失效：改写 index.json 后下一次调用正确感知（n=5001）。
- 写路径：剔除某项 `generation` 后 `list_items` 触发回填并持久化，
  内存值与磁盘值一致（验证惰性 mutable+`_save_index` 链路）。
- 隔离性：`_load_clean_index_mutable` 修改不污染共享缓存；
  全部 10 处读路径调用点逐一人工核对无嵌套突变。
- `has_local` 读路径缓存命中正常。

### 性能结果

| 指标 | 改动前 | 改动后 |
| --- | --- | --- |
| `list_items` 分页 @5000 | ~143ms | **~49ms（≈2.9×）** |
| `_load_clean_index` | ~54ms/次 | 首载 ~55ms，命中 ~0.04ms |
| 图片资产读路径（has_local/get） | 每次 ~54ms 解析 | 缓存命中 ~0ms |

注：收益同时覆盖**每张图片的 has_local/get_bytes 读路径**——
图库页加载 N 张缩略图原先各付一次全量解析，现为一次。

### 验证汇总

- `compileall` 全项目 ✅；`import main` 116 路由（docs 开启）/
  112（默认关，T-05 预期）✅
- `git diff` 审查：仅 `services/image_storage_service.py`
  +54/−24，无密钥、无无关改动 ✅
- 未提交 git；回滚方式：`git checkout services/image_storage_service.py`
  （该文件本阶段前无其他未提交改动）

### 遗留

- 真实图库页面并发压测、WebDAV 模式下 sync/retention 全链路
  需真实环境手工验证（本地无图床凭据）。
- `_load_clean_index` 首载仍 ~55ms（clean pass 对全量有效项仍
  需 normalize 校验）——如需进一步优化可将 clean 结果按
  `(mtime,size)` 持久化到旁路缓存文件，属更大改动，暂不做。

---

## Phase 2 · T-06 日志脱敏收敛实施记录

**目标**：消除 `utils/log.py` 与 `utils/diagnostics.py` 双轨脱敏，
收敛为 diagnostics 单一所有者。

### 实施内容

- `utils/diagnostics.py` 新增日志专用脱敏区块（+158 行）：
  - `sanitize_log_value()` 入口：先 `_collect_diagnostic_values`
    （扩展 `is_secret_key` 谓词收集字面量密钥值与代理值），再递归
    `_scrub_log_value` 清洗。
  - 密钥键规则 `_is_log_secret_key`：诊断精确集 ∪ `dx` ∪
    `key`/`*_key` 后缀 ∪ 子串族
    (`token|secret|password|passwd|credential|authorization|cookie|
    apikey|api_key`)；归一化兼容 `-`/空格/大小写。
  - base64/data-URI 屏蔽从 log.py 迁移（`_mask_log_base64`/
    `_is_log_base64_string`/`_mask_log_data_urls`），输出格式不变。
  - 字符串统一走 `sanitize_diagnostic_text`（新增覆盖：URL
    userinfo、Bearer、Authorization 头、query/key=value 凭据）。
  - `_collect_diagnostic_values` 增加可选 `is_secret_key` 谓词并
    统一空格归一化（diagnostics 默认行为不变，仅覆盖面略增）。
- `utils/log.py` 精简为壳（−64/+11 行净删）：删除全部 mask 实现与
  `base64/binascii/re` 导入，`_sanitize` 委托 `sanitize_log_value`；
  `Logger` 公共接口、日志格式 `[LEVEL] msg`、`_enabled` 不变。

### 验收与回归（实测）

- 验收标准：`{"password","api_key","cookie"}` → 全部 `[credential]` ✅
- 覆盖族：access/refresh/session token、client_secret、private_key、
  dx、authorization、api-key/Access Token/Cookie 变体 ✅
- 字面量密钥在自由文本中被替换（`login failed for hunter2` →
  `[credential]`）✅
- base64/data-URI 输出格式与旧实现一致 `(base64 len=N)` ✅
- int/非敏感值透传；`max_tokens:100`(int) 不误遮（与旧行为一致：
  旧实现也只 mask str 值）✅
- `monkey` 等不含密钥子串的键不误伤 ✅
- `scrub_diagnostic_value` 回归抽查不变 ✅
- `compileall` ✅；`import main` + 真实 logger 调用脱敏生效 ✅

### 行为差异（有意为之，记录备查）

- token 类字符串值由"前 10 字符+..."改为整体 `[credential]`（更安全）。
- 日志字符串新增 diagnostics 正则覆盖（此前完全没有）。
- `password/secret/api_key/cookie/authorization` 等键名由不遮改为遮。

### 回滚

`git checkout utils/log.py utils/diagnostics.py`（diagnostics 的
`_collect_diagnostic_values` 签名变更向后兼容——新增可选参数）。

---

## Phase 2 · T-07 WebDAV 客户端加固实施记录

**目标**：消除 http 明文凭据风险、无界响应内存、明确代理边界。

### 现状核查结论（修正任务卡假设）

- **session 泄漏不成立**：全部 5 处 `WebDAVClient` 实例化点
  （put/get/delete-batch/sync/test）均已 `try/finally session.close()`，
  评审 09 的"夸大"判断正确，无需改动。
- 真实缺口：`get()` 无界 `bytes(response.content)`、settings 保存
  无 scheme 校验、http 无任何提示。

### 实施内容

| 项 | 改动 | 位置 |
| --- | --- | --- |
| 响应上限 | 新增 `WEBDAV_RESPONSE_MAX_BYTES=50MB`（对齐
  `MAX_IMAGE_REFERENCE_BYTES` 领域约定）；`get()` 改 `stream=True`
  + Content-Length 预检 + `iter_content` 计数双保险 + `finally close` | `image_storage_service.py:29,248-263` |
| scheme 校验 | `_validate_image_storage_settings` 增加
  `urlsplit().scheme ∈ {http,https}`，拒绝 ftp/javascript/无 scheme | `config.py:412-414` |
| http 警告 | `test()` 成功且 scheme==http 时返回
  `warning` 字段（additive，前端旧版本忽略不报错） | `image_storage_service.py:276-284` |
| 代理边界 | 类 docstring 注明 WebDAV 独立于账号代理体系
  （管理员自有基础设施，低代理需求）——采纳任务卡"维持独立+文档化"建议 | `:182-187` |

### 验证（`/tmp/verify_t07.py`，本地 mock WebDAV server）

- PUT/GET/DELETE/MKCOL 正常闭环 ✅
- 60MB 响应被 Content-Length 预检拒绝（BrokenPipe=客户端按预期中止）✅
- scheme 校验：`https`/`http://内网IP` 通过；`ftp`/`javascript:`/无
  scheme 拒绝 ✅
- `test()` http → `ok=True` + warning 文案 ✅
- `compileall` ✅、`import main` 112 路由 ✅

### 未验证/遗留

- 真实 WebDAV 服务（Nextcloud/Alist/NAS）兼容性需手工验证；
  chunked 传输（无 Content-Length）走 `iter_content` 计数分支，
  逻辑同路径未单独构造用例。
- 前端设置页暂不展示 warning（additive 字段已就绪，UI 提示属
  可选增强，未纳入本任务最小范围）。

### 回滚

`git checkout services/image_storage_service.py services/config.py`
（注意：image_storage_service.py 含 T-09 未提交改动，整体回滚会
同时撤销 T-09——如需仅撤 T-07 请人工分离 hunk）。

---

## Wave 2 — T-16 / T-19 / T-26 / T-27 / T-29 卫生、部署加固与文档收尾（已完成，待用户验收）

执行日期：2026-11-14。范围：生产 print 收敛、包元数据、版本徽标、
安装脚本核查、Compose 健康检查、deployment.md 加固与安全边界、
ruff 引入、CI 冒烟。**未提交 git**（按规则等待用户授权；工作区
存在多个并行任务的未提交改动，本波次仅触碰授权文件）。

### T-16 卫生项

- `api/support.py` account-watcher 4 处 `print` → `utils.log.logger`
  （info×2 / warning×1 / error×1），改为 dict payload 走
  `sanitize_log_value` 脱敏；`result["errors"]` 内 token 字段已由
  `anonymize_token` 预匿名化，logger 再兜底。未改动该文件其他逻辑
  （auth_throttle / ClientIPMiddleware 为并行任务改动，保持原样）。
- `services/json_file.py`：5 处 stderr warning 保留，新增模块注释
  说明其为低层持久化兜底、有意绕过 logger（utils.log → config →
  storage 依赖链，底层模块不能依赖日志栈）。
- `services/storage/factory.py`：启动 print 保留，注释说明选定
  Application Database 属关键启动信息、不受 log_levels 过滤影响。
- 生产路径 print 复查（`api/ contracts/ services/ utils/ main.py`）：
  仅剩上述两处有意保留 + `conversation.py:1154` 注释行；
  `scripts/verify_oauth_refresh.py` 为手工运维脚本，不在生产路径。
- `pyproject.toml` `description` 占位符 → README.md 首行中文简介
  （与 `readme = "README.md"` 语言一致）。
- `README.md` / `README_EN.md` 版本徽标与 Release 链接
  v3.2.3 → v3.2.9（VERSION=3.2.9 为准）；「固定安装 v3.2.3」
  示例保留——其用途是演示 `--branch` 固定安装，非当前版本声明。
- `deploy/install.sh` 核查：`BRANCH` 环境变量 / `--branch` 已支持
  tag（`git clone --branch` 与 `default_image()` 均按 tag 处理，
  `v*` tag 会映射到同名镜像 tag），无需代码改动；
  `docs/deployment.md` 补充正式部署应固定 `BRANCH=<tag>`。

### T-19 部署加固

- `docker-compose.yml` app 服务新增 healthcheck：探测公开端点
  `GET /version`（无认证），镜像内无 curl，使用系统 `python3` +
  `urllib.request`；interval 30s / timeout 10s / retries 3 /
  start_period 60s（覆盖首次启动 entrypoint 的 `uv sync`）。
  另附**注释掉的** `deploy.resources` 限制示例（cpus/memory），
  不强制。
- `docs/deployment.md`：补充 `./config.json` 绑定挂载必须先存在
  （否则 Docker 创建同名目录导致配置不可读）；新增「反向代理与
  HTTPS」小节——公网部署必须 TLS 反代，含 Caddy / Nginx 示例，
  并提示公网域名下设置 `CHATGPT2API_BASE_URL`。

### T-27 ruff 引入

- `uv add --dev ruff` → dev 依赖组 `ruff>=0.17.5`（uv.lock 同步）。
- `[tool.ruff]`：line-length 120、target-version py313、
  `select = ["E9", "F"]`（仅语法错误 + Pyflakes，保守无噪音）。
- CI verify job 新增两步（位于 Compile backend 之后）：
  `uv run ruff check` 与 `CHATGPT2API_AUTH_KEY=ci-smoke uv run
  python -c "import main"`（后者为 T-26 冒烟）。
- **ruff 基线**：E9+F 共 20 个既有问题（16×F401 未用导入、
  4×F841 未用变量），分布于 12 个非本任务授权文件
  （`api/__init__.py`、`api/call_contract.py`、`services/
  account_service.py`、`services/config.py`、`services/
  image_task_service.py`、`services/protocol/*` 等），未做
  mass-fix。CI lint 步骤暂以 `continue-on-error: true` 告警引入，
  待基线清理后应移除该标记恢复阻断门禁。

### T-29 文档化收尾

- `docs/deployment.md` 新增「安全边界」一节，全部经代码核实：
  - 管理员密钥（`CHATGPT2API_AUTH_KEY` / `config.json auth-key`）
    = 完全信任级别（可管上游凭据、设置、用户密钥、更新与备份）。
  - 用户密钥吊销窗口：认证快照约 5s 刷新尝试、最长 30s 旧状态
    （`auth_service.py` 常量 `_AUTH_SNAPSHOT_REFRESH_INTERVAL_
    SECONDS=5.0` / `_AUTH_SNAPSHOT_MAX_STALE_SECONDS=30.0`）；
    同进程修改立即生效。
  - 单实例假设：认证快照、实时监控、图片任务、watcher 均为进程内
    状态，明确不支持多实例/多副本部署。
  - 公开下载端点（`/images/`、`/image-thumbnails/`、`/files/`）
    无认证，依赖不可猜测 URL：可编辑文件为 UUID 存储路径，
    图片为「日期 + 时间戳 + 内容哈希」路径，链接按 bearer 秘密对待。
  - 密钥轮换：管理员密钥改 env 需重启 / 改 config.json 约 2s
    生效（`_AUTH_KEY_CACHE_TTL_SECONDS=2.0`）；用户密钥走控制台
    `/api/auth/users` 新建-分发-禁用/删除，吊销约 30s 内生效。
- `CONTEXT.md` 未修改：该文件是领域术语表，无 protocol /
  backend_api 模块边界小节；模块边界归 `docs/maps/` 所有，
  按文档所有权规则跳过。

### 验证

| 检查 | 结果 |
| --- | --- |
| `compileall api contracts services utils main.py` | ✅ 通过 |
| `CHATGPT2API_AUTH_KEY=test import main` | ✅ 112 路由（docs 默认关） |
| `ruff check api/support.py services/json_file.py services/storage/factory.py main.py` | ✅ 0 问题 |
| `ruff check` 全仓（E9,F） | ⚠️ 20 个既有基线问题（见 T-27） |
| 生产 print grep | ✅ 仅 2 处有意保留（均有注释说明） |
| YAML 解析（docker-compose×2 + workflow） | ✅ 通过 |
| Docker build | 未执行（本波次为 yaml/文档级改动，按要求不跑） |

## Wave 3 — Phase 2/3 并行实施收尾（T-08/T-11~T-15/T-17/T-18/T-20/T-28，待用户验收）

本轮按用户指令并行执行剩余无决策门禁任务。5 个工作流：主线（T-08/T-13/T-17）+ 4 个后台代理（T-11/T-20、T-12、T-14/T-15、T-16~T-29 批次、T-18/T-28）。全部未提交。

### T-08 认证失败限流（SEC-09）

- 新增 `services/auth_throttle.py`：进程内失败计数，按「客户端 IP + token 哈希前缀」
  计数（不存原始凭据），5 次失败后指数退避返回 `retry_after`；成功认证清除状态；
  有界状态表防内存膨胀。
- `api/support.py`：`require_identity` 在认证前查 `retry_after`（命中 → 429 +
  `Retry-After` 头），失败 `record_failure`，成功 `record_success`；新增
  `ClientIPMiddleware`（纯 ASGI）把客户端 IP 写入 contextvar——仅当直接对端为
  loopback 时采信 `X-Forwarded-For` 最后一跳，防伪造头绕过限流。
- `api/app.py` 注册中间件；login/identity 两条认证路径共用同一限流器。
- 验证：状态机单测 5×401→429、成功清除、Retry-After 递增；`import main` 通过。
- 已知边界：进程内状态，单实例假设（已在 T-29 文档化）。

### T-11 配置缓存（PERF）

- `services/config.py`：`auth_key` 属性加 2s TTL 无锁缓存（immutable tuple +
  原子赋值，GIL 下安全）；env/config.json 双来源覆盖；变更最坏 ~2s 生效。
  `reload_if_changed` 未动。每请求一次 `config.json` 磁盘读降为 ~0.5 次/秒。

### T-12 输入边界（SEC-13 补齐）

- `api/ai.py`：6 个请求模型加 `Field(max_length)` + `model_validator` 统一边界——
  messages/input ≤1000、modalities ≤16、tools ≤256、base64_images ≤16、
  序列化载荷 ≤256MiB（迭代估算早退，不物化 json.dumps）。超限 → 422。
- `services/image_service.py` `download_images_zip`：≤500 文件、≤2GiB 累计
  （`HTTPException(413)`，先于存储查找）。
- 256MiB 取值理由：前端重发最近 32 条含图消息历史，单图 base64 上限 ~13MB，
  合法多轮视觉会话可达数百 MB；权威原始 body 闸门仍在反代层（范围外）。

### T-13 utils→services 循环依赖解除（CQ）

- `utils/log.py`：删除 `services.config` 导入，新增 `set_log_level_provider`
  注入点；`services/config.py` 底部注册 `lambda: config.log_levels`。
- `utils/helper.py`：删除 `services.proxy_service` 导入，新增
  `set_remote_fetch` 注入点；`services/proxy_service.py` 底部注册
  `_proxied_remote_fetch`（保留 `build_session_kwargs()` 代理语义）。
- `parse_image_count` 签名改为 `maximum` 关键字参数，消除函数内惰性
  `from services.config import config`；3 处调用点（ai.py/image_inputs.py/
  image_tasks.py）同步传 `config.image_max_n`。
- AST 静态扫描：`utils→services`、`utils→api` 导入边 = **0**；
  compileall + `import main`（112 路由）通过。

### T-14 诊断上下文合并（TD）

- `services/account_operation_events.py` 新增 `collect_import_error_context`
  （服务层所有者——放 utils 会重建 utils→services 边）。
- `cpa_service._import_error_context` 与 `sub2api_service` 诊断上下文构造
  委托共享函数，各自保留 CPA/Sub2API 私有注入（token 缓存、server secret）。
- 输出与旧实现逐字段 parity 验证通过（嵌套凭据、代理、空输入、wrapper）。

### T-15 except 约定（CQ）

- `.codex/rules/backend.md` 新增规则：清理/回退路径用
  `contextlib.suppress(Exception)`；新增宽泛 `except Exception` 必须
  log / re-raise / 带理由注释三选一。

### T-17 兼容别名审查（TD）

- `services/log_service.py`：`LogService`/`LogCursorMismatch`/`log_service`
  别名**保留**（外部集成可能按旧名导入），加注释标注 T-17 结论；
  未发现其他需清理的兼容别名。

### T-18 `__del__` 资源释放审查（PERF/稳定性）

- 全仓 `OpenAIBackendAPI` 实例化点逐一核查：8 类站点有显式
  close/with/context-manager 覆盖；4 个 `text_backend()` 消费点仅靠 GC
  兜底——判定**无真实泄漏**（壳实例 session 不发请求，`__del__`→`close()`
  链完整，curl_cffi Curl handle 由 `Session.close()` 释放）。**零代码改动**。

### T-20 CORS 配置化（SEC，决策 D5 取推荐默认 B）

- `api/app.py`：新增 `CHATGPT2API_CORS_ORIGINS`——空/缺省 = 同源（默认安全），
  逗号分隔精确 origin 白名单，`"*"` 显式恢复旧通配行为。
- `.env.example` 文档化该变量及通配风险。实测：默认 preflight 拒绝、
  配置 origin 放行、evil origin 拒绝、`*` 模式恢复。

### T-28 部分实施（PERF-06 已做，PERF-05/08 按证据跳过）

- `api/image_inputs.py`：新增 `_response_content_bytes()`——无 `iter_content`
  回退路径在 `bytes()` 拷贝**前**按 `MAX_IMAGE_REFERENCE_BYTES`(50MB) 拒绝；
  `_read_response_limited` 顶部加 Content-Length 预检（对齐
  `_download_image_url` 既有模式）。8/8 专项验证通过。
- PERF-05（`openai_backend_api` 4 处 `time.sleep`→Event）：**跳过**——无
  cancel Event、无并发 close 路径，Event 只增复杂度；sleep 均有 monotonic
  deadline 约束，审计原判"可接受现状"。
- PERF-08（accounts zip 流式化）：**跳过**——admin-only + `build_export_items`
  已先实体化全部 items，流式 zip 不消除主要分配。

### 并行期并发冲突与仲裁

- `api/app.py`：CORS（Agent B）+ ClientIPMiddleware（主线）共存，已复核。
- `api/support.py`：限流（主线）+ watcher print→logger（Agent D）共存。
- `api/image_inputs.py`/`api/ai.py`/`api/image_tasks.py`：T-12 边界 +
  `parse_image_count` 签名变更（消除 utils→services 残留边）三方一致。
- `services/config.py`：auth_key 缓存（B）+ WebDAV scheme 校验（T-07）+
  log level provider 注册（T-13）三处共存，import 已上移消除 E402 风险。
- 路由数 112 全程一致（docs 默认关闭 −4，为 Phase 1 预期）。

### 最终验证汇总

| 检查 | 结果 |
| --- | --- |
| `compileall api contracts services utils main.py` | ✅ |
| `CHATGPT2API_AUTH_KEY=smoke import main` | ✅ 112 路由 |
| `utils→services/api` 静态导入边 | ✅ 0 |
| `set_remote_fetch`/`set_log_level_provider` 注册点 | ✅ proxy_service:1154 / config:908 |
| `parse_image_count` 全部调用点签名一致 | ✅ 3/3 传 maximum= |
| `ruff check`（E9,F） | ⚠️ 20 个**既有**基线（16×F401+4×F841），本波新增 0；本波触及文件中的 2 处均为 HEAD 既有 |
| CI lint 门禁 | `continue-on-error`（待基线清理后转阻断） |

### 未实施（决策门禁，待用户裁决）

- **Phase 4 结构性重构**（D3 模块拆分幅度 + D11 测试基线前置）
- **T-25** 凭据字段级加密（D1）+ ADR
- **T-26 方案 B**（测试策略变更，D2 修正后为 Phase 4 门禁）
- **T-19 非 root 容器**（D6）
- T-09d verify_existing TTL 节流（候选遗留）
- ruff 20 项基线清理（建议按文件所有者分批，17 项可 `--fix` 自动修）

## Wave 4 — Phase 4 结构性重构 + 测试基线入库（已完成，待 E2E 验收）

用户决策：测试入库批准 / Phase 4 全部四项 / E2E 后发布。分支策略确立：
`dev` 为常驻开发+发布分支，`main` 仅跟随 upstream（yukkcat）。

### 测试基线入库（`0dcda4a`）

- `.gitignore` 移除 `tests/`；`backend.md` 修订：覆盖公共 Interface 的
  回归测试入库，临时脚本仍不入库；CI verify job 新增 `pytest tests/`。
- 契约基线 24 项 + 图片纯函数护栏 11 项 = **35 项全绿**。

### T-22 api/accounts.py → api/accounts/（`1c0cd45`）

2508 行 → 7 模块包（`_shared` 1140 + accounts/groups/operations/
remote_import/auth_keys + facade）。`create_router` 按原装饰器顺序聚合
`register_*`；43 条 APIRouter 路由逐一比对路径/方法/签名/顺序一致。

### T-21 account_service.py → account_service/（11 commits，`a1633c3`~`9614fd7`）

5303 行 → 12 模块，**Mixin 组合方案**（零调用点改写）：
`_shared`(异常/别名) `_primitives`(39纯方法) `_core`(init+状态+CAS写路径)
`_progress` `_mutations`(19) `_refresh`(15) `_remote_check`(14)
`_selection`(12) `_sync`(2) `_memory`(1) `_pool`(14) + `__init__.py`
组合 `AccountService` + 单例 wiring + 6 名 re-export。
150 方法全部归位；3 处 `AccountService.` 字面量改为 Mixin 名；
函数内 import（openai_backend_api/proxy_service/DATA_DIR）全部保留。

### T-23a protocol 层包化（`ed6dc6a` + `a1a6bfa`）

- `conversation.py` 3279 行 → 7 模块包；146 个顶层名字 facade 全量
  re-export（含 `ImageGenerationError` de-facto re-export）；
  `_generate_single_image` 662 行**verbatim 搬迁**（nonlocal 合法）。
- `openai_backend_api.py` 3894 行 → 9 模块包（constants/core mixin/
  memory/conversation_mgmt/search/editable/image_results/codex + facade）；
  133 方法中 130 verbatim；8 处 `OpenAIBackendAPI.` 字面量经
  staticmethod→classmethod+`cls.` 修复。

### T-23b / T-24 处置

- T-23b（`SingleImageRunner` 状态对象重构）：**暂缓**——阶段 A 已
  verbatim 搬迁，状态机隐式依赖需独立评估+图片链路手工回归，
  不阻塞当前交付。
- T-24：任务卡本身为"改动触及时拆分"策略，无专项重构。

### 验证

| 检查 | 结果 |
| --- | --- |
| pytest tests/ | ✅ 35 passed |
| compileall api contracts services utils main.py | ✅ |
| import main 路由数 | ✅ 112 |
| ruff check 全仓 | ✅ 0（T-23a 后补修 2 处测试文件未用 import） |
| 公共 Interface | ✅ 6+146+5 re-export 名全部可导入 |
| 最大单文件 | account_service/_mutations ~1000、_remote_check ~1000、openai_backend_api/_editable ~850 —— 均 <2000 验收线 |

### 未验证（需 E2E）

真实 OAuth/上游图片/记忆/分组/批量链路——`docs/ai-analysis/12-e2e-validation-dev.md`。

## Wave 5 — T-23b 状态机重构 + T-19 非 root 容器（已完成，待 E2E 验收）

### T-23b `SingleImageRunner` 状态对象（`6e61593` + `da8e156`）

- 规格：`docs/ai-analysis/15-t23b-runner-design.md`；实现仅改
  `services/protocol/conversation/_runner.py`（+691/−599）。
- 结构：`@dataclass SingleImageRunner` 承载跨迭代状态，
  `@dataclass ImageSlotAttempt` 承载每轮 attempt 上下文；
  `while True` 状态机保留在 `run()`，`_acquire_token` /
  `_resolve_account` / `_open_backend_and_egress` / `_consume_stream` /
  `_normalize_image_error` / `_finalize_image_slot` /
  `_retry_with_different_account` 为阶段方法；
  `_generate_single_image` 签名与返回契约不变。
- 10 项语义核对全部保留（attempt 每轮重建、fallback-proxy continue
  跳过 finalize、retry_error 传播、pending_switch 写回时序、
  monitor_attempt 共享 request、account_email 晚绑定、
  finally egress/backend 清理、image_attempts 累计）；
  mock 冒烟 3 场景（成功/切号/代理回退）通过。
- 有意偏差：`ImageSlotAttempt.stream_started`（原闭包 cell 必需）；
  方法命名沿用原闭包名。
- E2E 待验路径：message_as_error、pool 耗尽 retry_error、
  quota_consumed、ImageEgressDeadlineError、text_review。

### T-19/D6 非 root 容器（`39dc908`）

- `Dockerfile`：新增 `app` 用户（UID/GID 1000，`APP_UID`/`APP_GID`
  build-arg 可调）、`chown -R app:app /opt/chatgpt2api /app`
  （cp -a 同 UID 属主保留在 `set -eu` 下的前提）、
  `ENV HOME=/home/app UV_PYTHON_PREFERENCE=only-system
  UV_CACHE_DIR=/tmp/uv-cache`、`USER app:app`。
- `docs/deployment.md`：新增"非 root 运行与宿主机权限"章节——
  存量部署一次性属主迁移（data/ chown、config.json 可读、
  命名卷重建或 helper 容器改属主）、Linux UID 失配风险、
  Docker Desktop 免处理说明、NET_BIND_SERVICE 依赖。
- 可行性评估结论：全部运行时写入集中于 `/app` 命名卷与
  `/app/data` 绑定挂载，非 root 可行；端口 80 依赖 Docker 默认
  能力集（当前 compose 未 cap_drop）。

### 验证

| 检查 | 结果 |
| --- | --- |
| pytest tests/ | ✅ 35 passed |
| ruff check | ✅ 0 |
| import main 路由数 | ✅ 112 |
| upstream/main 同步 | ✅ main 与 upstream/main 零分叉（0/0） |

### 未验证（需 E2E/部署冒烟）

真实图片生成/编辑/切号/代理回退链路（T-23b）、非 root 容器实际
启动与卷写入（T-19）——均需真实环境，清单见
`docs/ai-analysis/12-e2e-validation-dev.md`。

## Wave 6 — 生产环境 E2E 验证（dev 实例，已完成）

部署形态：生产服务器 8.216.26.242（阿里云东京 ap-northeast-1，x86_64）
旁路实例 `chatgpt2api-dev`，`127.0.0.1:3001`，镜像
`registry.abyssalfish.com/deepsea/chatgpt2api:45c9cf8...`（amd64，
经 docker load 直传——东京↔国内链路 ~150KB/s，registry 直拉同速）。
生产旧实例 :3000 未动。

### 自动化验证（服务器 curl 实测）

| 项 | 结果 |
| --- | --- |
| §1.1 `/docs` 默认 | 404 ✓ |
| §1.3 无 key | 401 + 正确错误体 ✓ |
| §1.4 限流 | 5×401 → 429 + `Retry-After: 1` ✓ |
| §1.5 正确 key | 不受限流 200 ✓ |
| §1.6 CORS | 无 ACAO（同源默认）✓ |
| §2.2 chat completions | `auto` 正常返回+usage ✓ |
| §2.3 images generations | gpt-image-2 → PNG 56s ✓（T-23b 真实链路）|
| §2.4 images edits | multipart 参考图 → PNG 33s ✓ |
| §2.7 调用日志 | 3 条记录完整，无 auth-key 泄露 ✓ |
| 日志脱敏（实测） | `image_attempt_failed` 中 `request_token:"[credential]"` ✓ |
| 运行时日志 | 零异常/traceback；T-23b 遥测事件正常 ✓ |
| 非 root（实测） | uid=1000 运行、entrypoint/uv sync/卷写入全通 ✓ |

### 用户侧验证

- CPA JSON 导入账号（Plus）成功；Studio UI 全流程无问题反馈。
- n=1 生成在图库产生 2 张候选图——初步判断为上游返回多候选
  的固有行为（两次独立 conversation 流），待与旧版对比确认。

### OAuth 问题（搁置→已诊断）

现象：OAuth 流程中提示"身份验证令牌已失效"（OpenAI 侧过期
transaction 提示，非本项目文案）。`/api/accounts/oauth/start`
返回 200，无 exchange 报错日志。容器内实测 auth.openai.com /
auth0.openai.com / api.openai.com TLS 握手均正常（HTTP 边缘
响应，非证书错误）——**T-01 verify=True 未引入回归**，
判定为上游 OAuth transaction 过期（刷新/超时），建议用户
择机重试；CPA/Sub2API 导入路径已验证可用。

### 结论

E2E 实质通过：安全行为、真实上游文本/图片/编辑链路、账号管理、
日志脱敏、非 root 部署全部验证。OAuth 重试为遗留观察项。
