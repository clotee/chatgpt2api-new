# T-21 执行设计 — `account_service` 包化（Mixin 方案）

来源：结构分析报告（2026-11）。文件已从 `services/account_service.py`
原样移至 `services/account_service/_all.py`（行号不变）。

## 方案

**Mixin 组合**：所有跨方法调用已是 `self.<method>()`，方法块原样搬进
各 Mixin 类即零调用点改写。`_all.py` 中 `class AccountService` 的
bases 列表随提取逐步增长；全部完成后 `_all.py` 只剩类定义+wiring，
拆为 `_core.py`（core 方法）+ `__init__.py`（组合+wiring+re-export）。

## 模块与提取顺序

| 步 | 模块 | Mixin 类 | 内容 | 依赖 |
| --- | --- | --- | --- | --- |
| 1 | `_shared.py` | — | 模块级：`_RemoteCheckMarker`/`_CredentialGeneration` 别名 + 4 异常类（L44-103） | 无 |
| 2 | `_primitives.py` | `AccountPrimitivesMixin` | ~40 纯静态/类方法（L198-1305 区域的 normalize/谓词/JWT/CAS 标记） | 仅 utils |
| 3 | `_progress.py` | `RefreshProgressMixin` | 进度追踪 9 方法 + 类属性 `_refresh_progress`/`_refresh_progress_lock`/`_refresh_progress_last_pruned_at`（L137-139）+ 常量（L114-116） | core.get_account |
| 4 | `_mutations.py` | `AccountMutationMixin` | add/update/delete/export ~19 方法（L2132-3352、L5151-5244） | core+primitives |
| 5 | `_refresh.py` | `TokenRefreshMixin` | OAuth RT→AT ~16 方法（L1148-1832、L4576-4781） | +progress |
| 6 | `_remote_check.py` | `RemoteCheckMixin` | 远程校验/失效状态机/失败调度 ~14 方法（L2148-4359） | +refresh+mutations |
| 7 | `_selection.py` | `ImageSelectionMixin` | 选取/槽位 ~11 方法（L1853-2260、L4043-4138） | +refresh+remote_check |
| 8 | `_sync.py` | `AccountSyncMixin` | sync_accounts_and_quota 等 2 方法（L5027-5149） | +remote_check |
| 9 | `_memory.py` | `AccountMemoryMixin` | manage_account_memory（L4783-5025） | +refresh+progress |
| 10 | `_pool.py` | `PoolHealthMixin` | 池健康/自动移除/stats ~13 方法（L2278-2570、L5246-5294） | +sync+mutations |
| 11 | 收尾 | — | 剩余=core：`_all.py` 拆为 `_core.py`（`class AccountServiceCore(AccountPrimitivesMixin)` 含 `__init__`+常量+core 方法）+ `__init__.py` 组合类+wiring+re-export，删 `_all.py` | — |

方法→模块的完整清单见分析报告（method 名+行号区间已在上文
方法清单中给出；执行时以 `def ` 边界为准，行号仅供参考）。

## 关键规则

1. **方法块原样搬迁**：方法在类内 4 空格缩进，新 Mixin 类内同样
   4 空格——verbatim cut/paste，含装饰器（`@staticmethod`/
   `@classmethod`/`@account_processing_batch`）。
2. **类属性随类走**：`_refresh_progress*` 三个类属性放
   `RefreshProgressMixin`（`cls.` 访问经 MRO 仍共享）。
3. **类常量**（L109-135 的 ~20 个 `_XXX` 常量）留在 core 类上，
   `self./cls.` 访问经 MRO 不受影响。
4. **3 处字面量 `AccountService.` 引用必须改**：
   - `_all.py` ~L1120 `AccountService._now()`（→ primitives 内改
     `AccountPrimitivesMixin._now()` 或 `self._now()`）
   - ~L1126 `AccountService._decode_jwt_payload(...)`（同上）
   - ~L2604 `AccountService._account_payload_token(item)`（mutations
     静态方法内 → `AccountMutationMixin._account_payload_token`）
5. **函数内 import 必须保持函数内**（循环依赖安全）：
   - `_refresh.py`：`curl_cffi.requests`、`services.proxy_service.proxy_settings`
   - `_core.py`：`services.config.DATA_DIR`
   - `_remote_check.py`：`services.openai_backend_api`（2 处）
   - `_memory.py`：`services.openai_backend_api`（1 处）
6. **每个新模块**：`from __future__ import annotations` +
   `TYPE_CHECKING` 不需要（签名都是字符串注解）+ 按需 import
   （编译错误迭代补齐）。
7. **`_all.py` 的 `class AccountService` bases**：每步追加对应
   Mixin，顺序无关（无方法名冲突，已验证 150 方法名唯一）。
8. **`__init__.py` 最终形态**：
   ```python
   from ._core import AccountServiceCore
   from ._progress import RefreshProgressMixin
   ...
   class AccountService(AccountServiceCore, AccountPrimitivesMixin,
                        RefreshProgressMixin, TokenRefreshMixin,
                        RemoteCheckMixin, AccountMutationMixin,
                        ImageSelectionMixin, AccountSyncMixin,
                        AccountMemoryMixin, PoolHealthMixin):
       pass
   # L5297-5303 wiring（proxy_management_service 绑定）
   # re-exports
   ```

## 每步验证（必须全绿才提交）

```bash
uv run python -m compileall -q services/account_service/
CHATGPT2API_AUTH_KEY=test-auth-key uv run pytest tests/ -q   # 24 passed
CHATGPT2API_AUTH_KEY=test-auth-key uv run python -c "
import main
from services.account_service import (account_service, AccountService,
    ImageAccountSelectionError, OAuthRefreshError,
    TerminalRefreshTokenError, RefreshCredentialsChangedError)
print(len(main.app.routes))"   # 112
uv run ruff check services/account_service/
```

## 提交约定

每个模块一个 commit：`refactor: extract account_service <domain> (T-21 step N)`
作者 BLACK·CLOTEE <clotee@foxmail.com>，含 Devin trailer。
全部完成后 push origin dev。
