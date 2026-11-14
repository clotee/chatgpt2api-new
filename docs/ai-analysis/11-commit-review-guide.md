# 提交审查导引 — `feat/security-stability-hardening`

两个提交待审：

| Commit | 内容 | 规模 |
| --- | --- | --- |
| `3ae9324` | feat: security and stability hardening（路线图 Phase 1-3 全部） | 47 文件 +4173/−291 |
| `f23f361` | chore: ruff F401/F841 基线清零 | 12 文件 +5/−19 |

审查建议按风险分层，**不必逐行看完**——重点是行为变化面。

## 第一层：安全语义变化（必看）

### 认证与限流（新增攻击面，最需要细看）

- `services/auth_throttle.py`（新文件，85 行）：进程内失败计数，
  `(IP, token[:8])` SHA-256 分桶，5 次失败指数退避（1s→60s 封顶），
  `_MAX_BUCKETS=8192` + `_sweep_locked` 防内存膨胀。**看点**：桶键不含
  完整 token（前 8 字符进哈希）；成功 `record_success` 立即清桶——
  意味着攻击者拿到正确 key 后该桶状态清零（正确行为）。
- `api/support.py`：`require_identity` 顺序为 **retry_after 检查 →
  认证 → 失败计数/成功清零**。429 分支在认证**之前**，携带
  `Retry-After` 头。`ClientIPMiddleware`：仅 loopback 对端才采信
  `X-Forwarded-For` **最后一跳**（`entries[-1]`）——反代场景取的是
  代理追加的真实客户端段，客户端伪造的前置段被忽略。
- `api/app.py`：中间件注册顺序 `ClientIPMiddleware` 在
  `CORSMiddleware` **之前**（add_middleware 后加者更外层——确认
  contextvar 在路由执行前已设置）。

### CORS / 文档开关（行为收紧，可能影响现有部署）

- `api/app.py`：`CHATGPT2API_CORS_ORIGINS` 空 = `allow_origins=[]`
  （同源）。**若用户此前依赖 `*` 跨域直连 API，升级后会断**——
  CHANGELOG 已记录，部署文档已更新。
- `/docs`、`/redoc`、`/openapi.json` 默认 404，`CHATGPT2API_ENABLE_DOCS`
  opt-in。路由数 116→112 即此原因。

### OAuth TLS（P0 修复）

- `services/oauth_login_service.py`：`verify=False` → `verify=True`，
  用户 `skip_ssl_verify` 配置保留为显式 opt-out。**自签/内网 OAuth
  场景需用户显式关闭**——属预期行为变化。

## 第二层：性能改动（验证 benchmark 结论）

- `services/image_storage_service.py`（T-07+T-09 同文件，**最复杂**）：
  - `_load_clean_index` mtime+size 缓存；读路径共享 dict，写路径
    `_load_clean_index_mutable()` deepcopy；`list_items` 惰性
    copy-on-write。**看点**：9 处写路径调用点是否都切对了 mutable
    版本（漏切 = 共享缓存被污染）；`_public_url_prefix` 每请求一次。
  - WebDAV `get()`：stream + Content-Length 预检 + iter_content
    50MB 上限；`_validate_image_storage_settings` scheme 白名单。
- `services/config.py`：`auth_key` 2s TTL 无锁缓存（immutable tuple）。
  **看点**：`_auth_key_cache` 只在 auth_key 属性写，其他 config
  字段不受影响；轮换 env 后最坏 ~2s 生效。

## 第三层：结构性改动（行为应等价，重点看接线）

- `utils/log.py` + `utils/diagnostics.py`：脱敏实现整体迁移，
  `sanitize_log_value` 为新入口。**看点**：键名谓词扩展是否过度
  （`token`/`secret` 子串族会命中 `tokenizer` 类无辜键名——有意
  从严）；int 值不遮蔽（用量指标不受影响）。
- `utils/helper.py` + `services/proxy_service.py`：remote fetch 改
  `set_remote_fetch` 注入，proxy_service 底部注册
  `_proxied_remote_fetch`。**看点**：若 proxy_service 未被导入则
  回退为无代理 `requests.get`——确认启动链路必然导入它
  （services 层总会加载）。
- `services/account_operation_events.py` + `cpa_service.py` +
  `sub2api_service.py`：诊断上下文收敛，parity 已验证。
- `api/ai.py`：6 个请求模型边界（max_length + model_validator +
  256MiB 载荷估算）。**看点**：`extra="allow"` 透传字段不受界
  （已知缺口，靠反代 body 限制兜底）。

## 第四层：机械性改动（扫一眼即可）

- `f23f361` ruff 清理：12 文件，删未用导入/死变量 +
  `api/__init__.py` 加 `__all__` 保留 re-export。
- `api/support.py` watcher print→logger、`services/json_file.py`/
  `storage/factory.py` 注释、`pyproject.toml` 描述、README 版本号、
  `docker-compose.yml` healthcheck、`.github/workflows` CI 冒烟、
  `docs/deployment.md` 安全边界章节。
- `CHANGELOG.md` Unreleased 9 条、`.env.example` CORS 文档、
  `docs/ai-analysis/*` 分析文档、`TODO.md`。

## 已知遗留（非缺陷，记录备查）

- 进程内限流/缓存均为单实例假设（T-29 已文档化）。
- `extra="allow"` 请求字段无边界（反代兜底）。
- ruff CI 门禁当前 `continue-on-error`，基线已清零——**建议把
  workflow 中该标记去掉恢复阻断**（一行改动，可随下个提交做）。
- tests/ 契约基线（24 项）按规则不入库；若批准测试入库需修订
  `backend.md` 规则 + `.gitignore`。
