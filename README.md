# API Contract Governance（接口契约与请求治理示例服务）

一个基于 FastAPI 的演示服务，为 HTTP API 提供三类可版本化、可解释的治理能力：

1. **接口版本化契约**：同一资源在多个版本下访问，字段演进不破坏旧调用方；
2. **写操作幂等**：重复与并发提交只产生一次真实副作用；
3. **乐观并发控制**：基于陈旧状态的覆盖写入被明确拒绝。

所有失败归一化为统一错误契约，每次请求带贯穿响应与日志的 `trace_id`。

---

## 1. 快速开始

```bash
uv sync --group dev        # 安装运行与测试依赖
uv run uvicorn main:app --reload   # 启动服务
uv run pytest -s           # 本地运行全部测试（-s 打印并发时序日志）
```

不依赖任何外部服务或凭据：资源存储与幂等存储均为进程内实现。

---

## 2. 版本策略

### 2.1 版本标识与协商入口

两种入口语义完全一致，可混用：

| 入口 | 形式 | 示例 |
| --- | --- | --- |
| URL 前缀 | `/api/{token}/notes/...` | `/api/v2/notes` |
| 请求头 | `Api-Version: {token}` | `Api-Version: v2` 配合 `/notes` |

`token` 支持两种形态：

- 主版本别名：`v1`、`v2`；
- 发布日期别名：`YYYY-MM-DD`，解析为该日期当天生效的版本
  （如 `2025-06-01` ≡ `v2`，`2024-06-01` ≡ `v1`）。

当路径与头部同时给出且指向**不同版本**时，请求语义有歧义，返回
`VERSION_INCOMPATIBLE`，不做任意猜测。

### 2.2 版本注册表

| 版本 | 状态 | 发布日期 | 说明 |
| --- | --- | --- | --- |
| v0 | `retired`（已停用） | 2023-01-01 | 保留样例，用于演示“已知但不支持” |
| v1 | `deprecated`（仍支持，已废弃） | 2024-01-01 | 响应字段 `id/title/body/revision` |
| v2 | `current` | 2025-06-01 | `body` 改名为 `content`，新增 `tags` |

- 访问 `deprecated` 版本时，响应带 `Deprecation: true` 与 `Sunset` 头。
- **未知版本**（拼写错误、未来版本号、早于首个发布日的日期）与
  **已停用版本**必须可区分：

| 情形 | HTTP | error.code |
| --- | --- | --- |
| 无法识别版本（如 `v9`） | 400 | `VERSION_NOT_FOUND` |
| 版本已知但已停用（`v0`） | 400 | `VERSION_UNSUPPORTED`（details.status=`retired`） |

### 2.3 契约演进保证

- **新增字段不改变旧版本语义**：v2 新增 `tags`，v1 响应严格投影为
  `{id,title,body,revision}`，永远不会泄漏 `tags/content`。
- **字段改名/删除不让旧调用方静默拿到错位数据**：v1 请求体中出现 v2 字段
  （`content`/`tags`）会被字段白名单拒绝，返回 `VERSION_INCOMPATIBLE` 并指出
  具体字段，而不是静默丢弃或错配到 `body`。
- 内部使用规范模型 `NoteRecord{id,title,body,tags,revision}` 存储，
  出入站均通过显式的版本视图（`parse_*` / `render`）转换。

---

## 3. 幂等语义

- 写请求（POST/PUT/DELETE）通过 **`Idempotency-Key`** 请求头声明幂等标识。
- 服务端对每个 key 保存：请求指纹（版本 + 规范化载荷的哈希）、首次响应的
  状态码与响应体、首次请求的 trace_id。
- **重复提交**（同 key 同指纹）：无论顺序还是并发，只执行一次真实副作用，
  其余请求重放首次响应（状态码、响应体逐字一致），日志标记为“命中首次结果重放”。
- **并发提交**：每个 key 独立加锁；并发线程在锁上排队，第一个执行者落盘结果，
  其余全部重放（测试中 8 并发 → `execution_count=1`）。
- **同 key 不同载荷**（指纹不一致）：返回 `422 IDEMPOTENCY_REUSE`，
  防止键被串用；被拒请求不产生副作用，也不占用首次结果。
- key 的作用域是全局的：路径入口与 `Api-Version` 头入口共享同一幂等存储，
  跨入口并发同样保证单次副作用。
- 未携带 `Idempotency-Key` 的请求按普通请求执行（演示服务保持简单；生产中
  建议对非安全方法强制要求）。

## 4. 并发（乐观锁）语义

- 每个资源带单调递增的 `revision`（创建后为 1，每次成功修改 +1）。
- PUT/DELETE **必须**携带 `If-Match: <revision>`：
  - 缺失：`428 PRECONDITION_REQUIRED`；
  - 非正整数：`400 VALIDATION_ERROR`；
  - 与当前 revision 不一致：`412 PRECONDITION_FAILED`，details 给出
    `expected_revision` 与 `current_revision`，拒绝陈旧写入；
  - 一致：提交成功并返回新 revision。
- 两个客户端基于同一旧 revision 并发更新时，恰好一个成功（200，revision+1），
  另一个收到 412；最终状态只有一次写入生效，不会互相覆盖。

---

## 5. 统一错误契约

所有错误（含框架层校验、路由未命中、未捕获异常）都归一化为：

```json
{
  "error": {
    "code": "PRECONDITION_FAILED",
    "message": "更新基于陈旧版本（期望 1，实际 2），已拒绝以防覆盖写入",
    "details": {"id": "note-1", "expected_revision": 1, "current_revision": 2},
    "trace_id": "8f3c..."
  }
}
```

| code | HTTP | 含义 |
| --- | --- | --- |
| `VALIDATION_ERROR` | 400 | 参数/请求体校验失败（details.fields 只含字段路径与错误类型） |
| `VERSION_NOT_FOUND` | 400 | 无法识别的接口版本 |
| `VERSION_UNSUPPORTED` | 400 | 版本已知但已停用 |
| `VERSION_INCOMPATIBLE` | 400 | 字段与协商版本不兼容，或路径/头版本冲突 |
| `NOT_FOUND` | 404 | 资源或路由不存在 |
| `STATE_CONFLICT` | 409 | 资源状态冲突（预留） |
| `IDEMPOTENCY_REUSE` | 422 | 幂等键被不同请求内容复用 |
| `PRECONDITION_FAILED` | 412 | If-Match revision 过期，陈旧写入 |
| `PRECONDITION_REQUIRED` | 428 | 写操作缺少 If-Match |
| `INTERNAL_ERROR` | 500 | 内部异常：**对外只有固定文案**，堆栈仅写服务端日志 |

错误响应不包含内部类名、堆栈、SQL、连接串等实现细节；但内部日志保留完整堆栈，
可通过响应中的 `trace_id` 关联。

### 追踪标识

- 每个请求自动生成 `X-Trace-Id`（或透传形态合法的入站值：`[A-Za-z0-9_-]{8,128}`，
  含换行等非法字符的入站值会被替换，防止日志/头注入）。
- `trace_id` 同时出现在**响应头**、**错误响应体**与**该请求的全部日志行**中。

---

## 6. HTTP 接口速览

```
POST   /api/{version}/notes          # 创建（可带 Idempotency-Key）
GET    /api/{version}/notes/{id}
PUT    /api/{version}/notes/{id}     # 需 If-Match；可带 Idempotency-Key
DELETE /api/{version}/notes/{id}     # 需 If-Match；204

POST   /notes            # 以上四个接口均有“无版本前缀 + Api-Version 头”版本
GET    /notes/{id}
PUT    /notes/{id}
DELETE /notes/{id}
```

调用示例：

```bash
curl -s -X POST localhost:8000/api/v2/notes \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: order-1001' \
  -d '{"title":"t","content":"c","tags":["a"]}'

curl -s -X PUT localhost:8000/api/v2/notes/note-1 \
  -H 'Content-Type: application/json' -H 'If-Match: 1' \
  -d '{"content":"c2"}'
```

---

## 7. 本地验证方法

```bash
uv run pytest                      # 运行全部 28 个用例
uv run pytest -s                   # 额外打印请求输入、并发时序与判定依据日志
uv run pytest tests/test_idempotency.py -v   # 只看幂等/并发重复提交
uv run pytest tests/test_concurrency.py -v   # 只看乐观并发/陈旧写入
uv run pytest tests/test_versioning.py -v    # 只看版本兼容
uv run pytest tests/test_errors.py -v        # 只看错误归一化与 trace
```

测试为纯本地测试（`fastapi.testclient` + `ThreadPoolExecutor`/`Barrier` 制造真实
多线程并发），无外部服务依赖。断言维度包括：

- **副作用次数**：仓储 `side_effect_count`、幂等存储 `execution_count`；
- **错误分类**：HTTP 状态码与 `error.code`；
- **最终状态**：资源内容与 `revision`；
- **响应一致性**：并发/重复请求响应体逐字相同。

### 代码结构

```
app/
  versions.py     # 版本注册表、日期别名、版本协商（未知/停用可区分）
  contracts.py    # v1/v2 请求响应视图、字段白名单、规范模型投影
  idempotency.py  # 每键锁 + 指纹 + 结果重放
  repository.py   # 内存库、revision 乐观锁、副作用计数
  service.py      # 编排：版本兼容校验 → 幂等执行 → 乐观并发 → 错误翻译
  api.py          # 两类协商入口、Idempotency-Key/If-Match 头、废弃头
  errors.py       # 统一错误信封与全部异常处理器
  tracing.py      # trace_id 生成/透传、ASGI 中间件
  logging_setup.py# 结构化日志（record 级 trace_id 注入）
tests/            # 版本、幂等并发、乐观并发、错误契约四组用例
```
