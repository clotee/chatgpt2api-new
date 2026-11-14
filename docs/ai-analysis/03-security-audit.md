# 03: Security Audit — chatgpt2api

Status: current（2026-11-13 只读审计；行号基于当日工作区）

方法：人工代码审查 + OSV.dev 漏洞库批量查询（PyPI/npm 生态精确版本匹配）+
GitHub Advisory 复核。标注 **[已证实]** / **[潜在风险]** / **[待验证]**。

## 摘要

| 级别 | 数量 |
| --- | --- |
| Critical | 1 |
| High | 4 |
| Medium | 6 |
| Low | 8 |
| Info | 4 |

应用层安全工程整体扎实（SSRF/路径/更新链路/凭据脱敏体系为亮点），
**最大风险集中在依赖版本滞后**和一处 OAuth TLS 缺陷。

---

## SEC-01 OAuth code→token 兑换禁用 TLS 校验 **[已证实] [Critical] [传输安全]**

- 位置：`services/oauth_login_service.py:196`（`_exchange_code`）
- 证据：
  ```python
  kwargs = proxy_settings.build_session_kwargs(impersonate="chrome", verify=False)
  ```
  显式 `verify=False` 透传给 `requests.Session`，无视 `skip_ssl_verify`
  设置；该请求 POST `code_verifier + code` 至 `/api/accounts/oauth/token`，
  返回 access/refresh/id token。
- 对比：AT 刷新（`services/account_service.py:1324`）、CPA
  （`services/cpa_service.py:203,251`）、Sub2API（`services/sub2api_service.py:336`）、
  R2（`services/backup_service.py:112`）、上游会话
  （`services/openai_backend_api.py:257`）均 `verify=True` 或跟随设置——
  **唯独此处写死 False**，大概率是绕过问题的遗留。
- 影响：中间人可窃取长期 `refresh_token` → 上游账号接管。
- 建议：改 `verify=True`；若确有绕过必要，注释说明并限定 host。
- 修改风险：低（单行）；若上游证书链有特殊问题需先定位根因。
- 验证：单测断言 session kwargs `verify is True`；走一遍 OAuth 导入。

## SEC-02 starlette 1.0.0 存在 10 项已知漏洞 **[已证实] [High] [依赖安全]**

- 位置：`uv.lock` → `starlette==1.0.0`（fastapi 0.136.0 的传递依赖；
  最新版 1.10.0）。OSV 查询命中 10 条 advisory。
- 与本项目直接相关的：
  - **GHSA-82w8-qh3p-5jfq / CVE-2026-54283（High, CVSS 7.5）**：
    `request.form()` 对 `application/x-www-form-urlencoded` 静默忽略
    `max_fields`/`max_part_size` → 字段数驱动事件循环阻塞（~1M 字段
    子 10MB body 卡秒级）、字段大小驱动内存。**本项目
    `api/image_inputs.py:195` 对所有非 JSON content-type 调
    `await request.form()`**——`/v1/images/edits` 等端点可被携带
    urlencoded body 触发；需有效 User Key（`require_identity`），
    属认证后 DoS。
  - **GHSA-86qp-5c8j-p5mr / CVE-2026-48710（Moderate，CISA KEV）**：
    Host 头未校验导致 `request.url.path` 与实际路径不一致 →
    基于 `request.url` 的路径安全判断可被绕过。**本项目
    `api/errors.py:26,34,44` 用 `request.url.path` 做 OpenAI/Anthropic
    错误格式路由**；`api/support.py:50` 用 Host 头生成图片 base URL。
    实际影响有限（仅错误格式混淆 + 生成链接投毒），但模式命中。
  - **GHSA-jp82-jpqv-5vv3（Low）**：path 拼入 authority 污染
    `request.url.hostname`，fixed 1.3.0。
  - 另有 GHSA-x746（HTTPEndpoint getattr，项目未用该类→不适用）、
    GHSA-wqp7（StaticFiles UNC/Windows，项目用自研 FileResponse+Linux
    →不适用）。
- 建议：`uv lock --upgrade-package starlette` 至 ≥1.3.1（覆盖全部
  相关修复）；确认 fastapi 0.136 兼容性。
- 修改风险：低（锁文件升级）；需回归表单解析与错误响应格式。
- 验证：OSV 复查 starlette 目标版本 0 命中；`/v1/images/edits`
  multipart 与 JSON 两种 content-type 回归。

## SEC-03 pillow 12.2.0 明显滞后，OSV 命中 ~88 项 **[已证实] [High] [依赖安全]**

- 位置：`uv.lock` → `pillow==12.2.0`（`pyproject.toml` 约束 `>=12.2.0`；
  最新版 13.0.0）。
- 证据：OSV 批量查询返回 88 条 advisory（含 PYSEC-2026-xxxx 系列）。
  抽样确认：`PYSEC-2026-3493`（mmap 行距 OOB 读，fixed 12.3.0）、
  `GHSA-22hg-r758-rrw9`（MIC n_frames 无限循环，fixed 13.0.0）、
  `PYSEC-2026-4326`（fixed 13.0.0）等。Pillow 解析不可信图片格式
  （BMP/TIFF/MIC 等），单个畸形文件即可触发崩溃/越界。
- 影响面：项目处理**不可信图片**——用户上传（`api/image_inputs.py`）、
  上游返回图片、图库资产；`_image_dimensions`/`convert_image_bytes`/
  压缩/放大链路均过 Pillow。DoS（worker 崩溃）与潜在内存破坏。
- 建议：升级 `pillow` 至 ≥13.0.0（跨大版本，需验证 API 兼容：
  `Image.MAX_IMAGE_PIXELS`、`Image.Resampling`、格式常量）。
- 修改风险：中——大版本升级，图片处理全链路需回归（生成/编辑/图库/
  压缩/放大/SVG）。
- 验证：`uv lock --upgrade-package pillow`；`pytest` 图片用例 +
  各格式图片 smoke；OSV 复查。

## SEC-04 python-multipart 0.0.26 存在已修复漏洞 **[已证实] [High] [依赖安全]**

- 位置：`uv.lock` → `python-multipart==0.0.26`（最新 0.0.33）。
- OSV 命中：`GHSA-5rvq-cxj2-64vf`/`PYSEC-2026-3036`（querystring 分号
  分隔导致二次方解析 CPU DoS，CVSS High，fixed 0.0.30）、
  `GHSA-6jv3-5f52-599m`（分号参数走私，fixed 0.0.30）等 10 项。
- 影响面：multipart 表单解析（图片上传）路径直接相关。
- 建议：升级至 ≥0.0.30。
- 修改风险：低。
- 验证：OSV 复查；multipart 上传 smoke。

## SEC-05 anyio 4.13.0 传递依赖含 TLS 相关漏洞 **[已证实] [Medium] [依赖安全]**

- 位置：`uv.lock` → `anyio==4.13.0`（经 fastapi/starlette/httpx 传递；
  最新 4.16.0）。
- OSV 命中：`GHSA-82r6-8w77-94w6`（TLSStream IDNA 2003 主机名编码可致
  TLS 证书欺骗，fixed 4.14.2——**影响所有出站 TLS 连接**：上游
  ChatGPT、CPA、Sub2API、WebDAV、R2）、`GHSA-5p39-cfhj-2xmp`
  （process-pool stderr 阻塞，本项目未用进程池→不适用）。
- 建议：`uv lock --upgrade-package anyio` 至 ≥4.14.2。
- 修改风险：低。
- 验证：OSV 复查；出站连接 smoke。

## SEC-06 上游账号凭据明文落库 **[已证实] [High] [敏感信息]**

- 位置：`services/storage/database_storage.py:27-41` `AccountModel`
  （`data` Text 列存完整账号 JSON：AT/RT/ID token、指纹、代理配置；
  `access_token` 独立列）。全库无应用层加密。
- 证据：`AuthKeyModel.data` 同理存 User Key 哈希（哈希本身可接受，
  但账号 JSON 为明文 token）。R2 备份有可选 openssl AES-256 加密，
  本地库反而无加密选项。
- 影响：获得 `data/chatgpt2api.db` 文件或 PG 读权限 = 全部上游账号
  凭据泄露 → 上游账号接管。Docker 部署中 `./data` bind mount 权限
  决定风险。
- 建议：短期——`docs/deployment.md` 明确 `data/` 目录权限（700/600）、
  磁盘加密、PG 访问控制要求；中长期评估字段级加密（需 ADR，影响
  备份/迁移/按 token 检索）。
- 修改风险：中（加密方案涉及迁移与备份兼容）。
- 验证：部署文档评审；（若加密）迁移脚本 + 备份恢复演练。

## SEC-07 日志脱敏双轨，通用 logger 覆盖面窄 **[已证实] [Medium] [敏感信息]**

- 位置：`utils/log.py:67-85` `Logger._sanitize` vs
  `utils/diagnostics.py` `sanitize_diagnostic_text`/`scrub_diagnostic_value`。
- 证据：`utils/log.py` 仅按键名含 `token`/`dx` 或 base64/data-URI
  脱敏；`password`/`secret`/`api_key`/`cookie`/`authorization` 键名
  **不 mask**——调用方 `logger.info({"password": ...})` 会明文输出。
  诊断体系（操作事件/错误投影）脱敏完善，两套实现并存违反
  "唯一所有者"规则。
- 影响：未来新增日志点若传敏感 dict，凭据可进日志文件。
- 建议：`Logger._sanitize` 委托 `utils/diagnostics.py` 单一实现，
  并补齐常见密钥键名。
- 修改风险：低-中（日志输出变化，需确认无依赖旧 mask 行为的解析）。
- 验证：构造含 password/api_key/cookie 的 dict 走 logger 断言 mask。

## SEC-08 WebDAV 客户端与全局安全体系脱节 **[已证实] [Medium] [敏感信息/SSRF]**

- 位置：`services/image_storage_service.py:176-261` `WebDAVClient`。
- 证据：
  - 裸 `requests.Session()`（`:182`）不走 `proxy_settings`——绕过代理
    配置，也不受 `skip_ssl_verify` 约束；
  - `webdav_url` 允许 `http://`（`test()` 仅校验 scheme ∈ {http,https}
    `:249`）→ Basic auth 凭据与图片明文传输；
  - `get()`（`:236-238`）`bytes(response.content)` 全量入内存，**无大小
    上限**（对比图片 URL 抓取有 50MB/16 张/8 并发全限制）；
  - session 仅 `test()` 的 finally 中 close（`:261`），常规 put/get/delete
    不 close——连接泄漏；
  - 超时倒是有的（`_request`/`MKCOL`/`DELETE` 均 timeout=30）。
- 影响：admin 配置面；http:// WebDAV = 凭据+图片明文泄露路径；
  恶意/失控 WebDAV 服务器可返回超大响应耗尽内存。
- 建议：settings 校验对 `webdav_url` 强制 https 或显著警告；`get()`
  加响应大小上限（流式读取+上限）；统一 session 生命周期（上下文管理
  或按操作创建）；明确是否应走全局代理。
- 修改风险：低-中。
- 验证：设置页 http:// URL 校验提示；大文件 get 上限测试；
  session close 断言。

## SEC-09 认证接口无失败限流 **[已证实] [Medium] [访问控制]**

- 位置：`api/support.py:30-46` `require_identity`/`require_admin` →
  `services/auth_service.py` `authenticate`；`api/system.py:178`
  `/auth/login`。
- 证据：错误 key 无尝试次数限制/延迟/锁定。`sk-` 生成 User Key 有
  192bit 熵不可爆破，但**管理员手设 `CHATGPT2API_AUTH_KEY` 若弱可被
  在线枚举**（`install.sh` 默认 `openssl rand -hex 24` 强度足够，
  但用户可自定义弱值，`config.py` 启动仅检查非空 `_is_invalid_auth_key`）。
- 建议：对认证失败加按 IP+key 前缀的失败计数与指数退避（进程内即可，
  单实例假设）；文档要求 auth key 最小长度/熵；可选对连续失败返回
  429 + `Retry-After`。
- 修改风险：低-中（注意不要误伤正常高频 API 调用——限流应只针对
  **失败**认证）。
- 验证：连续错误请求触发退避；正常 key 不受影响。

## SEC-10 前端依赖存在已修复漏洞 **[已证实] [Medium] [依赖安全]**

- 位置：`web-vue/package-lock.json`。
- OSV 结果：
  - `axios==1.19.0` → 8 项 advisory（fixed 1.20.0）：HTTP/2 adapter
    代理/NO_PROXY 绕过、FormData header 注入、ReDoS、原型污染 gadget
    等。**注意：绝大多数为 Node adapter 问题，本项目 axios 仅运行于
    浏览器（XHR/fetch adapter）**，实际暴露面小；但升级成本极低。
  - `markdown-it==14.2.0` → 4 项 ReDoS/复杂度 advisory（fixed
    14.3.1-14.3.3）：linkify、link reference、smartquotes、inline HTML
    路径。**本项目用 markdown-it 渲染模型输出
    （`web-vue/src/lib/studioMarkdownRenderer.ts`，`html:false`）**——
    恶意/畸形 markdown 可冻结渲染线程（客户端 DoS）。prompt injection
    场景下上游输出可控性有限但非零。
  - `follow-redirects==1.16.0` → 3 项（fixed 1.16.1/1.16.2）：Node 端
    凭据头跨域泄露；axios 浏览器构建不含此依赖→不适用，Info。
  - `vite==7.3.6` → 5 项均为 dev-server/launch-editor（仅开发模式）
    →不适用生产，Info。
- 建议：`npm update axios markdown-it`（或精确升至 1.20.0/14.3.3+），
  `npm run build` 回归。
- 修改风险：低。
- 验证：OSV/npm audit 复查；Studio markdown 渲染 smoke。

## SEC-11 账号导出/凭据下发端点为敏感出口 **[已证实] [Medium] [敏感信息]**

- 位置：`api/accounts.py:1947-2022` `/api/accounts/export`；
  `:1279` `/{id}/access-token`；`:1296` `/{id}/refresh-token`。
- 证据：export 的 `txt` 格式直接拼接 `access_token` 行、`zip`/`json`
  含完整账号 JSON（含 token）；单项 token 端点返回原始 AT/RT。
  均 `require_admin`——**属设计内迁移功能，非缺陷**，但须认知其
  敏感性：任何拿到 admin key 的会话可批量拖库全部上游凭据。
- 建议：文档明确"admin key = 全部上游凭据"的信任级别；可选给
  export 增加二次确认或审计事件（`account_operation_events` 已有
  事件体系可挂接）。
- 修改风险：低。
- 验证：审计事件记录断言。

## SEC-12 SVG 内联响应可执行脚本 **[已证实] [Low] [XSS]**

- 位置：`services/image_service.py` `get_image_response`（约 42-43 行）、
  `get_thumbnail_response`（约 105-106 行）。
- 证据：`.svg` 以 `image/svg+xml` 内联返回且无 `Content-Disposition`；
  浏览器直接打开可执行 SVG 内 `<script>`。内容为系统生成/上游返回，
  但上游若返回恶意 SVG（或未来接入不可信来源）形成存储型 XSS。
- 建议：SVG 响应加 `Content-Disposition: attachment` 或
  `Content-Security-Policy: sandbox`/`default-src 'none'` 头，
  或对 SVG 做消毒（项目已有 `utils/svg.py` 校验基础）。
- 修改风险：低。
- 验证：直接访问 .svg URL 响应头断言。

## SEC-13 列表类请求字段无上限 **[已证实] [Low] [输入校验]**

- 位置：`api/accounts.py:83-143` `AccountCreateRequest.tokens`/
  `accounts`/`access_tokens` 等 `list` 字段无 `max_items`；
  `api/ai.py` `ChatCompletionRequest.messages` 无条数/体积上限
  （`ConfigDict(extra="allow")` 接受任意扩展字段）。
- 证据：admin/user-key 均可触发；无全局 body-size 中间件，
  Starlette `request.json()`/`request.form()` 全量入内存。
- 影响：超大 body 内存放大（认证后）；import 列表无界解析。
- 建议：按业务上限加 `max_length`；`messages` 加条数与总字符上限；
  评估反代层 body-size 限制并写入部署文档。
- 修改风险：低。
- 验证：超限请求 422。

## SEC-14 `download_images_zip` 内存无上限 **[已证实] [Low] [资源耗尽]**

- 位置：`services/image_service.py:314-346`。
- 证据：全量 `read_bytes` 进 `BytesIO` ZIP，无聚合上限（对比单图
  50MB/输入总量 100MB 限制，输出方向无对应约束）。
- 建议：限制单次打包文件数/总字节，或改临时文件流式打包。
- 修改风险：低。
- 验证：大批量打包内存测试。

## SEC-15 Sub2API `base_url` 无 scheme 校验 **[已证实] [Low] [传输安全]**

- 位置：`services/sub2api_service.py:144-160` `_normalize_server`。
- 证据：仅 `_clean` 文本规整；配 `http://` 时登录密码明文传输。
  admin 配置面。
- 建议：保存时校验 `https://`（或允许 http 但强警告）。
- 修改风险：低。
- 验证：非法 base_url 校验提示。

## SEC-16 admin key 比较非恒定时间 **[已证实] [Low] [侧信道]**

- 位置：`api/support.py:25` `_legacy_admin_identity`：`token == auth_key`。
- 影响：理论时序攻击面，实际风险极低（网络噪声 >> 比较差异）。
- 建议：改 `hmac.compare_digest`（一行）。
- 修改风险：无。验证：代码审查。

## SEC-17 FastAPI 文档端点默认暴露 **[已证实] [Low] [信息泄露]**

- 位置：`api/app.py:114` `create_app`——`/docs`、`/openapi.json`、
  `/redoc` 默认开启。
- 影响：泄露完整 API 结构（低敏感，但扩大攻击面测绘）。
- 建议：生产部署关闭（`docs_url=None, redoc_url=None,
  openapi_url=None`）或环境变量开关。
- 修改风险：低。验证：关闭后 404。

## SEC-18 CORS 全开 **[已证实] [Low] [访问控制]**

- 位置：`api/app.py:116-123`：`allow_origins=["*"]`、
  `allow_credentials=False`。
- 影响：Bearer 认证下无 cookie CSRF；但任何来源 JS 拿到 key 即可调用，
  放大泄露面（配合 localStorage token 存储）。
- 建议：改配置化允许来源（默认同源），或文档说明取舍。
- 修改风险：低-中（前端同源部署不受影响；跨域 API 消费方需配置）。
- 验证：跨域请求行为回归。

## SEC-19 部署面加固项 **[已证实] [Low] [运维安全]**

汇总已证实的部署层事实（详见 TODO.md P2）：

- 容器以 root 运行（`Dockerfile` 无 `USER`）；
- 无内置 TLS（仅 HTTP:80），Bearer key 明文——必须反代 HTTPS；
- compose 无 app healthcheck/资源限制；镜像 tag `:latest` 未锁 digest；
- `docker-compose.yml` 挂载 `./config.json`——本地无文件时 Docker
  会创建为**目录**导致挂载异常（`deploy/install.sh` 有预写兜底，
  手工路径需注意）；
- `deploy/install.sh` 支持 `curl|bash` 且内部 `curl|sh` 装 uv、
  按分支名从 raw.githubusercontent 拉 compose 文件（可变引用）；
- `proxy_runtime.skip_ssl_verify` 为用户显式配置项（默认关）；
- `resolve_image_base_url`（`api/support.py:50`）信任 Host 头生成
  公开链接——伪造 Host 仅污染该响应（叠加 SEC-02 CVE-2026-48710
  后果仍限于链接投毒）。

## SEC-20 Auth 快照多副本一致性窗口 **[已证实] [Info] [访问控制]**

- 位置：`services/auth_service.py:21-22`：5s 刷新节流 + 30s 最大陈旧。
- 影响：多实例部署时禁用/删除的 User Key 最长 ~35s 才在所有副本生效；
  单实例无影响。项目隐含单实例假设（monitor/image task 均进程内存）。
- 建议：文档化该窗口与单实例假设。
- 修改风险：—（文档项）。验证：文档评审。

## SEC-21 公开资源端点靠不可猜 URL **[已证实] [Info] [设计取舍]**

- `/images/`、`/image-thumbnails/`、`/files/`、`/version` 无需认证，
  靠随机路径保护；路径校验已到位（`normalize_image_relative_path`
  + `relative_to`）。属设计取舍，路径含 uuid/日期结构不可枚举。
- 建议：如需更强保护可在反代层加规则；无需代码改动。

## SEC-22 Bearer key 无过期/会话机制 **[已证实] [Info] [设计取舍]**

- 静态 key 认证，无过期/轮换/会话失效；吊销靠禁用/删除 User Key。
  `last_used_at` 有记录（`auth_service.py`），利于审计。
- 建议：文档说明轮换流程；如需更强可加 key 过期策略（ADR 级）。

## 已核查为安全的区域（非问题，供对照）

- **SQL 注入**：SQLAlchemy + 参数化 `text()`，无字符串拼接 SQL。
- **路径遍历**：`normalize_image_relative_path` 拒绝 `..`/盘符/冒号；
  `image_local_path` resolve+relative_to 双重校验；`/files/` 强制
  3 段结构+扩展名白名单；`resolve_web_asset` 同样双重校验。
- **SSRF（图片 URL）**：`api/image_inputs.py` 协议/公网 IP/逐跳重定向
  重验/DNS 钉住（CURLOPT_RESOLVE）/50MB+16 张+8 并发/3 重定向上限——
  业界少见的完整实现。
- **更新链路**：`services/update_service.py` 仅 GitHub 域名 HTTPS、
  SHA-256 校验、tar 防 `..`/符号链接、体积/文件数上限、版本清单
  匹配、失败回滚。
- **子进程**：全部参数数组无 `shell=True`；PG 密码走 `PGPASSWORD`
  env、备份口令走 env，不落命令行。
- **OAuth 桥**：PKCE S256、state 内嵌 session+nonce、10min TTL、
  64 会话上限、成功才消费会话。
- **User Key 存储**：SHA-256 哈希 + `hmac.compare_digest` + CAS 乐观锁。
- **settings 敏感字段**：`sensitive=True` 元数据 + `has_*` 投影 +
  空值丢弃 + revision CAS。
- **前端 XSS**：markdown-it `html:false`，`v-html` 仅用于渲染后
  Markdown（StudioMarkdownContent.vue），代码高亮走 highlight.js。
- **CSRF**：纯 Bearer 认证无 cookie，天然免疫经典 CSRF。
- **JSON 文件原子写**：`json_file.py` tmp+os.replace+.bak 回退。
- **并发写**：SQLite `BEGIN IMMEDIATE`+行锁+revision CAS；PG
  advisory lock；文件锁 flock。

## 待验证项

1. `pytest`/`npm run test:runtime` 未运行（tests 目录 gitignore 且
   本地不存在）——以上代码级结论基于静态审查，动态行为未实测。
2. Pillow 88 项 advisory 中部分可能为 OSV 数据噪声（PYSEC 批量条目），
   已抽样确认多条真实；建议 `pip-audit`/`uv` 复核后定级。
3. `request.form()` urlencoded 攻击面（SEC-02）需有效 User Key——
   若部署暴露给不可信 API 消费者，风险升级；纯 admin 自用则降级。
