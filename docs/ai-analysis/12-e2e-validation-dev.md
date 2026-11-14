# dev 分支 E2E 验证指南

针对 `dev` 分支（main + 安全/稳定性加固 + 测试基线）交付前的手工验证。
自动化部分已由 `tests/`（24 项）+ CI 覆盖，本指南只列**必须人工确认**的项。

## 0. 前置准备

```bash
# 本地源码方式
git checkout dev && git pull
uv sync
CHATGPT2API_AUTH_KEY=<你的管理员key> uv run uvicorn main:app --port 3000

# 或 Docker 方式（验证 compose healthcheck 新增项）
docker compose up -d --build
docker compose ps   # app 服务应变为 healthy（约 60s start_period 后）
```

需要：至少 1 个可用上游账号（控制台已导入）、可选 WebDAV/NAS、可选公网域名+反代。

## 1. 安全行为验证（约 10 分钟）

| # | 操作 | 预期 |
| --- | --- | --- |
| 1.1 | `curl http://localhost:3000/docs` | **404**（默认关闭） |
| 1.2 | `CHATGPT2API_ENABLE_DOCS=1` 重启后再访问 | 200 |
| 1.3 | 不带 key 请求 `GET /api/accounts` | 401 `{"error":"密钥无效或已失效"}` |
| 1.4 | 连续 6 次错误 key 请求同一端点 | 前 5 次 401，第 6 次起 **429** + `Retry-After` 头 |
| 1.5 | 换正确 key 立即请求 | 200（正确凭据不受限流影响） |
| 1.6 | 浏览器跨域直连 API（非同源 Origin） | 无 `Access-Control-Allow-Origin`；配置 `CHATGPT2API_CORS_ORIGINS` 后对应 origin 放行 |
| 1.7 | 控制台正常使用（同源） | 无 CORS 报错，功能不受影响 |

## 2. 真实链路验证（需上游账号，约 20 分钟）

| # | 操作 | 预期 |
| --- | --- | --- |
| 2.1 | OAuth 登录导入账号 | 正常完成（TLS 校验已恢复；自签环境需账号配置 `skip_ssl_verify`） |
| 2.2 | `/v1/chat/completions` 文本对话 | 流式响应正常，含图片历史的多轮对话正常 |
| 2.3 | `/v1/images/generations` 生成图片 | 返回 URL/base64 正常，图库可见 |
| 2.4 | 图片编辑 `/v1/images/edits` + 参考图 | 正常 |
| 2.5 | Studio 控制台：对话画图、历史会话、图片下载/zip | 全部正常 |
| 2.6 | 账号管理：刷新 AT、记忆开关、分组、批量操作 | 正常（T-21 拆分后的核心回归面） |
| 2.7 | 日志页查看调用记录 | 正常；日志中 password/api_key/cookie 类字段显示 `[credential]` |

## 3. WebDAV 验证（可选，有 NAS 时）

| # | 操作 | 预期 |
| --- | --- | --- |
| 3.1 | 设置 WebDAV `https://` 地址 + 测试连接 | 成功 |
| 3.2 | `http://` 地址测试连接 | 成功但返回 `warning` 字段提示明文 |
| 3.3 | 非 http(s) scheme（如 `ftp://`）保存设置 | 被拒绝（400） |
| 3.4 | 上传/浏览/删除图片走 WebDAV | 正常；单文件 >50MB 被拒 |

## 4. 性能抽查（可选）

```bash
# 图库列表（本地图片多时对比手感；预期分页明显变快）
curl -H "Authorization: Bearer $KEY" "http://localhost:3000/api/images?page=1&page_size=50"

# 账号列表
curl -H "Authorization: Bearer $KEY" "http://localhost:3000/api/accounts?page=1&page_size=50"
```

预期：图库分页无感知卡顿（改动前 5000 项约 143ms → 现约 49ms）。

## 5. 部署形态验证（可选）

- [ ] `docker compose up -d` 后 `docker inspect` healthcheck 状态 healthy
- [ ] 公网反代 + HTTPS 下 `CHATGPT2API_BASE_URL` 设置正确，图片外链可访问
- [ ] `config.json` 挂载文件预先存在（防 Docker 建目录坑）

## 6. 签收

- [ ] §1 全部符合预期
- [ ] §2 全部符合预期（2.1/2.3/2.6 为高优先级）
- [ ] 发现问题记录：现象 / 复现步骤 / 相关 commit
- [ ] 全部通过后通知 Devin 进入发布流程（v3.3.0，需另行授权打 tag）
