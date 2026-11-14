# 09: Plan Review — 独立架构评审

Status: draft（评审意见，待用户决策；不构成实施批准）

评审对象：`06-improvement-roadmap.md`、`07-development-tasks.md`（及
`08-test-and-acceptance.md` 的验收可执行性）。
评审方式：逐项对照真实代码重新验证计划中的事实断言、依赖关系、
验收标准与工作量估算。评审人立场：独立、不预设原计划正确。

---

## 一、总体结论

**方向正确，但存在 3 处事实性错误、1 处结构性依赖错误、
若干验收标准不可执行和两处过度设计。**

计划的分阶段思路（先安全止血→再稳定性能→后架构债→结构性重构）
合理，P0/P1 安全任务（T-01~T-08、T-12~T-20 大部分）证据扎实、
风险评估基本准确。**主要问题集中在 Phase 2 性能任务的前提失真
和 Phase 4 的验收基础缺失**：

| # | 级别 | 问题 |
| --- | --- | --- |
| R1 | **高** | PERF-03 前提错误：账号列表**已是内存快照**，T-10 的"TTL 缓存"是重复建设 |
| R2 | **高** | PERF-01 前提失真：分页请求**已跳过 rglob**，真实瓶颈是索引 JSON 全量解析+跨进程锁，T-09 方案 B 过度设计 |
| R3 | **高** | 验收依赖不存在的测试设施：后端 `tests/` 为零、前端 `web-vue/tests/` 本地不存在且 `npm run test:runtime` 在新克隆必失败；Phase 4"pytest 全量"验收**无法执行** |
| R4 | **中** | 依赖图错误：D2（测试策略）被列为 Phase 5 可选决策，但 Phase 4 验收硬依赖它——应前置为 Phase 4 的门禁 |
| R5 | **中** | 多处验收标准不可验证或目标值错误（OSV"0 命中"、python-multipart 目标版本过时、P99 无测试工具、">2000 行"任意阈值） |
| R6 | **中** | T-13 方案选择不当：循环边实际只有 3 处且 2 处已是惰性导入，存在更简单修法；M 级风险评估与"utils 全仓引用"表述不匹配实际半径 |
| R7 | **低** | 工作量普遍略低估（T-08/T-21/T-22 未计入无测试网下的手工回归成本）；个别任务缺前置测量步骤 |

---

## 二、事实核查结果（计划断言 vs 代码实况）

以下逐条给出核查证据。✅=属实，⚠️=部分属实/需修正，❌=错误。

### 安全类断言

| 计划断言 | 核查 | 证据 |
| --- | --- | --- |
| T-01 OAuth `verify=False` 无条件 | ✅ | `services/oauth_login_service.py:196`，`build_session_kwargs(..., verify=False)` 直传 `requests.Session`；全仓其他凭据调用均 `verify=True` |
| T-02 starlette 1.0.0 有 10 项 OSV | ✅ | OSV 实测 10 项；修复版本最高 **1.3.1**（form limits GHSA-82w8 + PYSEC-2026-249）。**新增**：fastapi 0.136.0 `requires_dist` 为 `starlette>=0.46.0` **无上限**（PyPI 元数据实测），版本约束不阻塞升级，但 fastapi↔starlette 1.x 行为兼容仍需回归 |
| T-02 python-multipart≥0.0.30 | ⚠️ | OSV 实测 10 项，修复版本为 0.0.27/**0.0.30/0.0.31**——目标应为 **≥0.0.31**，计划写 0.0.30 会漏修最新一项 |
| T-02 anyio≥4.14.2 | ✅ | OSV 实测 4 项，fixed=4.14.2 |
| T-03 pillow ~88 项 advisory | ✅ | OSV 实测 **恰好 88 项**，fixed ∈ {12.3.0, 13.0.0}；升级 13.x 方向正确 |
| T-04 前端依赖 | ✅ | axios 1.19.0/markdown-it 14.2.0 低于修复线 |
| T-05 compare_digest `api/support.py:25` | ✅ | `_legacy_admin_identity` 用 `==` 比较 auth_key |
| T-05 `/docs` 默认开启 | ✅ | `api/app.py:114` `FastAPI(...)` 未传 `docs_url`/`openapi_url`，默认全暴露 |
| T-05 max_items 缺失 | ✅ | `api/ai.py:53,68` `messages: list[dict]` 无长度约束 |
| T-07 WebDAV 四缺陷 | ⚠️ 部分夸大 | 裸 `requests.Session()`、`get()` 全量 `bytes(response.content)` 无上限、`session.close()` 仅在 `test()`——属实；但 **"session 泄漏"被夸大**：`WebDAVClient` 在 5 处均为**按操作新建实例**（`image_storage_service.py:497/547/1006/1400/1468`），实例出作用域后 CPython 引用计数即回收，泄漏有界。真正的增量价值在 http:// scheme 校验和响应上限 |
| T-08 认证无失败限流 | ✅ | `require_identity`/`require_admin`→`auth_service.authenticate` 无失败计数；`/auth/login` 同 |
| T-12 zip 全量内存 | ✅ | `image_service.py:314` `download_images_zip` 全部 `get_bytes` 进内存 BytesIO，无条数/字节上限 |
| T-20 CORS `*` | ✅ | `api/app.py:118` `allow_origins=["*"]`，`allow_credentials=False` |

### 性能类断言（本次评审重点复核）

| 计划断言 | 核查 | 证据 |
| --- | --- | --- |
| PERF-01 "图库每请求 rglob 全量扫盘" | ❌ **前提错误** | `image_service.py:254` `refresh_index=not paged`——**前端 gallery.ts 带 page_size 分页，常规浏览 paged=True → 跳过 rglob**。rglob 仅剩三条路径：非分页调用（limit=0）、retention cleanup（周期任务，`:169`）、delete-all-matching。**真实瓶颈是另一处**：`_load_clean_index()`（`image_storage_service.py:310`）每次调用都**全量读+解析+清洗 index.json**，且整个 `list_items` 持有 `_index_lock`+**跨进程文件锁**（`:273-277` `_index_guard`），每个图库翻页都全量解析索引并阻塞所有写操作 |
| PERF-02 auth_key 每请求读盘 | ✅ | `config.py:485-489` `auth_key` property 每次 `read_json_object` 读 config.json，无缓存、不走 `reload_if_changed` |
| PERF-03 "账号列表每请求全量反序列化 JSON blob" | ❌ **前提错误** | `account_service.py:2263` `list_accounts` 遍历的是**内存 dict `self._accounts`**，由 `_refresh_accounts_snapshot_if_stale()`（`:426`）按 **5s TTL+revision** 刷新——JSON 反序列化在 `load_accounts_snapshot()` 里但**至多每 5 秒一次**，不是每请求。`api/accounts.py:1122` 的 `_account_for_api` 投影**只作用于当前页切片** `filtered[start:end]`，已分页。每请求真实成本 = N 个 `dict(item)` 浅拷贝 + `_filtered_accounts` 全量过滤，千级账号为毫秒级 |
| PERF-04 settings 1s 轮询 | ✅ | `reload_if_changed` 1s 节流属实，但 DB 轮询成本极低，计划已正确标注"已合理" |

### 架构类断言

| 计划断言 | 核查 | 证据 |
| --- | --- | --- |
| CQ-01 `utils→services` 包级循环 | ⚠️ 需精确化 | 实际边：`utils/helper.py:19` **顶层** `from services.proxy_service import proxy_settings`（唯一顶层边，且**仅 `:550` 一处使用**）；`utils/log.py:24`、`utils/helper.py:654` 为**函数内惰性导入**（import 时不形成环）。`utils/diagnostics.py` 是纯叶子（仅 re/typing），T-06 委托安全 |
| 22 处 `except: pass` 均合理 | ✅ | 沿用前次人工核查结论 |
| T-21 account_service 5304 行/169 方法 | ✅ | 实测 5304 行、`def` 计数 164（169 含嵌套/属性，量级正确） |

---

## 三、逐项任务评审

### Phase 1 — P0 安全止血

**T-01 OAuth verify=False → True** — ✅ **批准建议**。事实、方案、回滚均正确。补充：验收中"手工走一遍 OAuth 导入"依赖真实账号，属 L4 手工项，08 文档已正确归类。

**T-02 依赖升级批次 A** — ⚠️ **批准但需修正**。
- 修正 1：python-multipart 目标 **≥0.0.31**（非 0.0.30）。
- 修正 2：fastapi 约束已查实 `starlette>=0.46.0` 无上限——可行性确认，但**必须实测 fastapi 0.136.0 + starlette 1.3.1 组合**（fastapi 与 starlette 内部 API 耦合，metadata 兼容≠行为兼容）。验收中"表单超限 urlencoded 被拒"断言方向正确，应具体化为 GHSA-82w8 的复现用例。
- 建议：starlette 与 anyio 可同批；python-multipart 单独一步便于定位（计划已建议，保留）。

**T-03 Pillow 13.x** — ⚠️ **批准但修正验收**。
- 88 项 advisory 数字属实，但其中包含 vendored 库（libtiff/libjpeg 等）条目；13.0.0 后**仍可能有未修复项**。
- **验收标准错误**：06 文档写"OSV 复查 0 命中"不可达。应改为"12.2.0 的 88 项中，升级后命中数显著下降，且列出仍命中项逐一评估可达性"。
- API 使用面核查属实（Image.open×12 等），大版本升级风险评估合理。

**T-04 前端依赖** — ✅ **批准**。axios/markdown-it 漏洞的浏览器可达性已在原审计中正确降级。

**T-05 琐碎安全包** — ⚠️ **批准但需补两点**。
- 子项 2（docs 开关）在任务卡内标注"需决策"但**未分配 D 编号**，与 06 决策表脱节——应并入决策表（建议新增 D9 或并入 D5）。
- 子项 5 Sub2API scheme：任务卡写"需决策"但未给选项细节；建议明确默认=允许 http 但响应/日志 warning（内网部署常见），https 强烈推荐。
- `messages max_length` 上限值需给出依据（建议按 token/字符双维度，先宽后紧）。

### Phase 2 — P1 稳定与性能

**T-06 日志脱敏收敛** — ✅ **批准**。`utils/diagnostics.py` 纯叶子已核实，委托方向无环；`utils/log.py` 仅 114 行、14 处引用，半径比 T-13 小得多，先做顺序正确。

**T-07 WebDAV 加固** — ⚠️ **批准但降范围**。
- "session 泄漏"证据不足（按操作新建+GC 回收），应从"泄漏"改述为"无显式 close 依赖 GC 时机"。
- 真实价值排序：http:// scheme 强制/警告 > get() 大小上限 > session 显式 close > 代理策略文档化。
- 代理策略"维持独立+文档化"的建议合理（WebDAV 是管理员自有基础设施）。

**T-08 认证失败限流** — ⚠️ **批准但需补设计约束**。
- 计划未处理**反向代理后真实 IP**问题：`require_identity` 拿不到 client IP 时，按 IP 计数会退化为全局计数（所有用户共享一个桶→误伤）。需明确：单机直连假设下用 `request.client.host`；经反代时需 `--proxy-headers` 或可信 X-Forwarded-For 配置——这应写入设计而非留白。
- 进程内限流与单实例假设一致，但**应与 T-10/T-11 的缓存一起文档化"单进程假设"**（uvicorn 无 --workers，docker-compose 单进程已核实）。
- 简化备选（供决策）：公网部署本就要求 HTTPS 反代，可在文档推荐 nginx `limit_req`，应用内限流降为"建议"而非"必须"。但应用内限流对无反代的内网部署仍有价值——**维持 P1，补 IP 设计**。

**T-09 图库索引优先** — ❌ **方案需重做，问题真实但药方错误**。
- 前提修正：瓶颈不是 rglob（分页已跳过），而是**每请求全量解析 index.json + 跨进程锁内执行**。
- 计划 Option B（写路径挂索引+周期对账）针对的是"rglob 成本"，对真实瓶颈（JSON 解析+锁）**无效**——写路径建索引后 `_load_clean_index` 仍要全量解析。
- **更简单方案（推荐替换）**：`_load_clean_index` 增加 **mtime+size 校验的进程内缓存**（index.json 未变则复用解析结果），`_index_guard` 持锁时间同步缩短。改动集中在 `image_storage_service.py` 一个文件，风险低，无索引一致性语义变化。
- Option A（TTL 节流 rglob）对真实瓶颈也无效，应删除。
- 建议：先加 `list_items` 耗时日志/计数（一次小改动）确认 index 规模与耗时分布，再实施缓存方案。工作量从 L 降为 **M**。

**T-10 账号列表快照缓存** — ❌ **建议撤销或降级为测量任务**。
- 前提错误（见事实核查）：账号已在内存快照（5s TTL+revision），投影已分页。再加 TTL 缓存是**对已缓存数据的二次缓存**，收益趋近于零，反而引入失效一致性风险（164 个方法的写路径失效覆盖无法保证）。
- 若担心千级账号的 `dict()` 拷贝+过滤成本，正确做法是**先压测**（`_accounts_page` 打点），确认 P99 超标再优化 `_filtered_accounts`（如关键词预过滤、缓存 group_names——后者每请求都查 `_account_group_names()`，可缓存）。
- 建议：改为"测量+按需优化"任务（S），从"必须实施"移出。

**T-11 配置读取优化** — ⚠️ **批准但收窄**。
- auth_key 每请求读盘属实。但注意 `auth_key` property **不走 `reload_if_changed`**，是独立的每次读盘路径——缓存方案应明确 TTL（≤5s）并与 settings 刷新策略解耦。
- settings 1s 轮询已合理，计划"写时失效+周期兜底"方向对，但应明确这是**可选优化**而非必须（DB 轮询成本极低）。
- 补充：`auth_key` 同时读 env+文件，env 优先——缓存键需包含 env 值。

**T-12 输入边界补齐** — ✅ **批准**。证据属实；补充建议：`messages` 上限应分层（条数+总字符），zip 上限建议条数+总字节双维度，数值给出依据并文档化可配置。

### Phase 3 — P2 架构与工程债

**T-13 解除循环依赖** — ⚠️ **方案需重选，风险评估偏差**。
- 事实修正：顶层环边仅 `helper.py:19` 一处（`proxy_settings` 仅 `:550` 使用）；另 2 处为函数内惰性导入（`log.py:24`、`helper.py:654`），**不构成 import 期循环**。
- 计划方案（"调用点移到 services 侧/新建 http_utils"）方向对但**工作量被高估**：最小修法是将 `:19` 改为 `:550` 同函数内惰性导入（与 `log.py` 现有模式一致），或把 `:540-560` 所在函数迁入 services——均为 S-M 而非 M。
- 但需决策：惰性导入是"消除 import 期环"还是"掩盖语义依赖"？架构规则倾向后者——utils 的 HTTP helper 需要 proxy config 本身是分层异味，**根治是把该函数迁到 services**（推荐），惰性导入仅作过渡。建议任务卡给出两方案对比（计划已有技术决策要求但未展开）。
- "utils 全仓引用"的风险表述夸大：改动半径实际是 3 个 import 边 + 1 个函数迁移。

**T-14 诊断逻辑合并** — ✅ **批准**。同构复制属实，提炼方向正确。

**T-15 except 约定** — ✅ **批准**。纯文档约定，无风险。

**T-16 print/版本/元数据/install.sh** — ✅ **批准**。注意与 T-05-6 的 print 清理有重叠，任务卡已标注"合并做"，执行时去重。

**T-17 兼容别名** — ✅ **批准**。需先确认外部消费者（计划已列前置）。

**T-18 `__del__` 审查** — ✅ **批准**。审查型任务，成本低。

**T-19 部署加固** — ⚠️ **批准但补验证前置**。
- compose healthcheck 可行：`/version`（`api/system.py:191`）无鉴权 GET，已核实存在。
- `./config.json` 挂载坑属实；建议补充核实 `config.json` 是否可选（若可选，文档给出不挂载方案更干净）。
- 非 root 容器（D6）：entrypoint 先 `uv sync` 再 exec uvicorn，`/app` 运行时卷 root 写入——非 root 化需先验证 entrypoint 每步的写权限，计划"需验证"表述正确，建议把验证做成任务内第 1 步而非假设。

**T-20 CORS 配置化** — ✅ **批准**。D5 决策点设置合理。

### Phase 4 — P2 结构性重构

**T-21/T-22/T-23 拆分** — ❌ **整体存疑：验收基础不存在 + 收益/风险比需重估**。
- **阻断性问题**：验收要求"pytest 全量+五链路 smoke"，但后端测试为零、前端 runtime 测试本地缺失。**在没有回归网的情况下拆分 5304 行核心凭据/调度模块，等于盲拆**——一旦引入回归，无任何自动化手段能发现。
- 计划把 D2（测试入库）放 Phase 5 可选，但 Phase 4 实际**硬依赖**它。这是依赖图错误（R4）。
- 建议三选一（供用户决策）：
  - **A（保守，推荐）**：Phase 4 前置一个"最小回归网"任务——哪怕只提交 5-10 个契约/冒烟测试（SSRF 拦截、路径遍历、账号 CRUD、图片任务状态机、OpenAI 端点 schema），也足以支撑拆分验收。这需要修订"测试不入库"规则（D2=B 的最小子集）。
  - **B**：Phase 4 整体推迟，先做完 Phase 1-3，待测试策略明确后再排。
  - **C**：维持现状但把 Phase 4 验收降级为"纯手工 smoke 清单+灰度观察"，风险自担——**不推荐用于 account_service 这种凭据核心**。
- 工作量重估：T-21 XL 可能仍偏低——164 方法密集互调，拆分顺序错误会引入新循环依赖；建议按"子域逐个抽离+每步独立回归"细化为 5-7 个子 PR 而非笼统 XL。
- T-23（conversation/backend_api）已标可选，维持。

**T-24 前端按需拆分** — ✅ **批准**。机会主义重构，无专项风险。

### Phase 5 — P3 策略项

**T-25 凭据加密** — ✅ **批准（决策门控正确）**。D1+ADR 前置设置合理；补充：加密字段需与 `account_view` 投影、备份服务、`access_token` 独立列的查询路径（`get_account_by_token_identity` 按 token 检索！）兼容——**按 token 查库意味着加密后无法直接 WHERE 匹配**，需引入 token 哈希索引列或解密后扫描，工作量可能被低估。建议任务卡补此约束。

**T-26 测试策略** — ⚠️ **批准但升级为前置决策**。见 R4：应从 Phase 5 移到 Phase 4 门禁。

**T-27 ruff** — ✅ **批准**。低风险。

**T-28 PERF 小优化** — ⚠️ **部分失效**。
- PERF-05（sleep→Event.wait）合理；
- PERF-06 回退分支——需先确认该路径真实存在且值得改；
- PERF-08 zip 流式——与 T-12 的上限方案重叠，二选一即可（上限更简单，流式收益边际）。

**T-29 文档化** — ✅ **批准**。

---

## 四、跨任务问题

### 4.1 测试设施缺口（最重要）

- 后端：`tests/` gitignore 且**本地不存在**，pytest 仅 dev 依赖，零测试文件。
- 前端：`web-vue/tests/` gitignore 且**本地不存在**；`package.json` 的 `test:runtime` 引用 21 个测试文件，**新克隆下该脚本必然失败**（文件不存在）——这比原审计"测试不入库"更严重：连仓库自带的验证脚本都是不可执行的。
- 后果：08 文档 L1/L2/L3 层级大面积落空；所有"pytest 全量""npm run test:runtime"验收标准不可执行；Phase 4 重构无安全网。
- **建议**：新增任务 **T-00 测试基线建设**（Phase 0 或 Phase 1 并行）：
  - 最小集：后端 pytest 冒烟（app 启动、路由表、3-5 个安全关键路径契约断言）；前端把 `test:runtime` 所需文件的最小子集补回或把 script 改为可选。
  - 决策依赖：D2 提前，至少确定"安全关键路径契约测试入库"这一最小集。
  - 若用户坚持测试完全不入库，则 Phase 4 必须推迟或降级验收——需明示此 trade-off。

### 4.2 验收标准可验证性问题

| 位置 | 问题 | 修正建议 |
| --- | --- | --- |
| 06 Phase 1 验收 | "OSV 复查目标版本 0 命中"对 pillow 不可达 | 改为"命中数较基线下降+逐项可达性评估" |
| T-02 | python-multipart≥0.0.30 漏 0.0.31 修复 | 改 ≥0.0.31 |
| 08 §六 | "P99 ↓≥50%"无测试工具/方法 | 指定工具（wrk/ab 或自写 asyncio 压测脚本）+ 具体端点+并发数+数据规模 |
| 08 Phase 4 | "无单文件 >2000 行"任意阈值 | 改为"按子域内聚度评审，无单一职责文件超过其自然边界"或给出拆分目标行数区间 |
| 全部任务 | "pytest 全量"不可执行 | 先建 T-00 或改为具体测试清单 |
| 08 §二 | OpenAPI 快照依赖 `/openapi.json`，T-05 关闭 docs 后消失 | 已在文中注明"先存档"，但应明确存档物的保存位置与格式 |

### 4.3 遗漏项

- **单进程假设未文档化**：T-08 限流、T-10 缓存（若保留）、T-11 配置缓存、auth snapshot 均为进程内状态；uvicorn 无 `--workers`（compose 已核实单进程）。多副本部署会使这些全部失效——应在 D4 或 T-29 中明确声明。
- **按 token 检索与加密冲突**（T-25）：`get_account_by_token_identity`/`get_account(access_token)` 需要 token 明文或等价索引，加密设计必须解决。
- **scripts/image_upscale Node 服务**：npm 依赖供应链未审计、未在任何任务覆盖——建议列入 T-29 文档化或单独评估。
- **备份恢复演练**：backup_service 有加密备份但无恢复验证任务——建议加入 08 E2E 清单（恢复到临时目录验证）。
- **docker base image digest 固定**：原审计低危项，未分配任务——可并入 T-19。

### 4.4 依赖图修正建议

```
现状:  T-01..T-05 (P0) → T-06..T-12 (P1) → T-13..T-20 (P2) → T-21..T-24 (P4) → T-25..T-29 (P5)
                              │                                    │
                              └──── D2(测试策略) 被错误地放 P5 ──────┘ Phase4 验收依赖它

建议:  T-00 测试基线(最小集) ──┬──→ Phase 1-3 各任务验收
                              └──→ D2 决策 ──→ Phase 4 门禁
```

具体调整：
1. 新增 **T-00**（Phase 0 或与 Phase 1 并行启动）：最小回归网。
2. **D2 从 Phase 5 提前**，至少在 Phase 4 开始前必须有结论。
3. **T-09 重做**（索引缓存方案，工作量 L→M）。
4. **T-10 降级**为测量任务或撤销。
5. **T-13 给出两方案**（迁函数 vs 惰性导入过渡）。
6. Phase 4 各任务增加前置："T-00 完成 + D3 确认拆分边界"。

### 4.5 工作量重估汇总

| 任务 | 原估 | 评审后 | 原因 |
| --- | --- | --- | --- |
| T-00（新增） | — | M | 从零建最小测试网 |
| T-08 | M | M+ | 需补 IP/代理设计+无测试网下手工回归 |
| T-09 | L | M | 方案简化为索引缓存 |
| T-10 | M | S 或撤销 | 前提错误，改测量任务 |
| T-13 | M | S-M | 实际仅 3 个 import 边 |
| T-21 | XL | XL+ | 拆 5-7 子 PR+无测试网回归成本 |
| T-22 | L | L | 维持 |
| T-23 | XL | XL | 可选，维持 |

---

## 五、需要用户决策的事项（评审新增/修正）

| 编号 | 事项 | 选项 | 影响 |
| --- | --- | --- | --- |
| **D9（新增）** | `/docs` 关闭策略 | A. env 开关默认关（开发可开）B. 直接关闭 C. 保持开启+文档说明 | T-05-2 |
| **D10（新增）** | Sub2API http:// base_url | A. 允许+warning B. 直接拒绝 | T-05-5、T-07 类似项 |
| **D11（新增）** | Phase 4 是否前置最小测试网 | A. 先建 T-00 再拆（推荐）B. Phase 4 推迟 C. 降级手工验收 | T-21~23、D2 |
| **D2（修正）** | 测试策略 | 从"Phase 5 可选"改为"Phase 4 门禁"；最小集=安全关键路径契约测试 | 全局 |
| **D3（修正）** | 拆分范围 | 建议明确"仅 account_service"，api/accounts 与 conversation/backend_api 视 Phase 4 后评估再定 | T-21~23 |
| D1/D4/D5/D6/D7/D8 | 维持原决策表 | — | — |

---

## 六、评审建议的修改优先级

**必须修改后再批准**：
1. 修正 PERF-01/PERF-03 前提错误，重做 T-09、撤销/降级 T-10（§三）。
2. 新增 T-00 测试基线任务；D2 提前为 Phase 4 门禁（§4.1/4.4）。
3. 修正验收标准：OSV"0 命中"、python-multipart 版本、P99 方法、行数阈值（§4.2）。
4. T-25 补充 token 索引约束（§三 T-25）。

**建议修改**：
5. T-13 给出双方案对比；T-07 修正"泄漏"表述；T-08 补 IP 设计；T-05 补 D9/D10。
6. 06 文档同步更新任务数（29→30 含 T-00）、Phase 2/4 验收条件。

**可不改**：Phase 1 安全任务主体、Phase 3 大部分工程债、Phase 5 策略项框架。

---

## 七、评审声明

- 本评审未修改任何业务代码；所有核查均为只读。
- PERF-01/PERF-03 的前提错误源自前序审计报告（02/04），责任在评审链条，本次已如实修正——这正是设置反向评审的价值。
- 本评审不批准实施；所有"批准建议"仅表示该任务方向与证据自洽，
  最终实施顺序与范围待用户按 §五 决策后确定。
