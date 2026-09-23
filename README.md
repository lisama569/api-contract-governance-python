# API 契约治理与请求治理服务（Python / FastAPI）

为一个 HTTP 资源服务提供三类可演进、可解释的治理能力：

1. **可版本化的接口契约**：同一资源可在多个 API 版本下被访问，新增/删除/改名字段不会让旧版本调用方拿到错位或被污染的数据；不支持的版本被明确拒绝。
2. **写操作幂等与并发控制**：相同幂等标识的重复或并发请求只产生一次真实副作用并回放一致结果；并发修改通过乐观锁识别，基于陈旧状态的更新被明确拒绝。
3. **归一化错误契约与追踪**：所有失败返回稳定可区分的结构，不泄漏内部堆栈；每个请求有贯穿响应头、错误体与日志的 `trace_id`。

本服务为**单进程内存实现**，不依赖数据库或外部服务，便于在本地直接运行与验证；真实部署时把 `DocumentStore` / `IdempotencyStore` 换成带持久化与跨进程协调的实现即可，契约语义不变。

---

## 1. 运行与本地验证

需要 Python ≥ 3.11（开发环境为 3.14）与 [uv](https://github.com/astral-sh/uv)。

```bash
# 安装依赖（含开发依赖 pytest）
uv sync

# 运行服务（自动重载）
uv run fastapi dev main.py
# 健康检查
curl -s http://127.0.0.1:8000/healthz

# 运行全部测试（无需任何外部服务或凭据）
uv run pytest
```

测试默认开启 `log_cli`，会实时打印：**请求输入、并发时序（屏障对齐 / 首个写入让出事件循环 / IN_FLIGHT 等待）、每个判定的依据**（如 `expected_revision` vs `actual_revision`、幂等 `verdict`）。

测试分布：

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_idempotency.py` | 顺序重复回放、**8 路真并发重复提交只一次副作用**、同键不同体冲突、并发重复更新 |
| `tests/test_versioning.py` | v1 不泄漏 v2 字段、改名字段在旧版本被拒、新增字段不改变旧语义、不支持/非法版本 |
| `tests/test_concurrency.py` | 陈旧更新被拒、缺少版本前提、v1 旧字段名乐观锁、**6 路并发更新 1 成功 5 冲突** |
| `tests/test_errors.py` | 错误信封稳定性、错误分类、trace_id 头/体一致、客户端透传、非法 JSON、无堆栈泄漏 |
| `tests/test_internal_error.py` | 未预期异常归一化为 500，堆栈只进日志且 trace_id 一致 |

> 并发测试使用 `httpx.AsyncClient` + `ASGITransport` 在**同一事件循环**里真正同时发请求（线程版 TestClient 会把请求串行化，无法覆盖 IN_FLIGHT 路径），并通过异步钩子让首个写入 `await asyncio.sleep(...)` 制造确定性的重叠窗口。

---

## 2. 资源与接口

资源为「文档 Document」。内部规范字段：`id, title, body, owner, tags, revision, created_at, updated_at`。

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/v{version}/documents` | 创建文档，请求体需含 `id`；支持 `Idempotency-Key` |
| `GET  /api/v{version}/documents/{id}` | 读取文档（按版本序列化） |
| `PUT  /api/v{version}/documents/{id}` | 整体替换，必须携带版本字段（v1 `rev` / v2 `revision`）；支持 `Idempotency-Key` |
| `GET  /healthz` | 健康检查 |

### 快速示例

```bash
# v2 创建
curl -s -X POST http://127.0.0.1:8000/api/v2/documents \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: create-doc-1' \
  -d '{"id":"doc-1","title":"标题","body":"正文","owner":"alice","tags":["a"]}'

# 同一资源用 v1 读取（字段名不同、且看不到 tags/时间戳）
curl -s http://127.0.0.1:8000/api/v1/documents/doc-1

# 带乐观锁更新（revision=0 表示"我基于 revision 0 修改"）
curl -s -X PUT http://127.0.0.1:8000/api/v2/documents/doc-1 \
  -H 'Content-Type: application/json' \
  -d '{"title":"标题","body":"新正文","owner":"alice","tags":["a"],"revision":0}'
```

---

## 3. 版本策略

- 版本通过 URL 路径段协商：`/api/v1/...`、`/api/v2/...`。当前支持 **v1、v2**，最新为 **v2**。
- 版本是**契约选择器**，同一底层资源可在任意受支持版本下访问。

### 字段契约

| 内部规范字段 | v1（旧） | v2（新） |
| --- | --- | --- |
| `title` | `title` | `title` |
| `body` | **`content`** | **`body`**（改名） |
| `owner` | **`created_by`** | **`owner`**（改名） |
| `revision` | **`rev`** | **`revision`**（改名） |
| `tags` | — 不存在 — | `tags`（新增） |
| `created_at` / `updated_at` | — 不存在 — | 存在（新增） |

### 兼容性保证

- **新增字段不影响旧版本**：`tags`、`created_at`、`updated_at` 只出现在 v2 响应；v1 响应字段集合恒定为 `{id,title,content,created_by,rev}`，旧调用方语义不变。
- **删除/改名不会静默错位**：v1 只接受 `content/created_by`。若向 v1 提交 v2 的字段名 `body/owner`，服务端以 `VALIDATION_ERROR` 拒绝并在 `details.unknown_fields` 中指出，而不是把数据塞进错误的字段。
- **向前未知字段同样拒绝**：向某版本提交该版本契约之外的字段（如未来的 `future_field`）会收到 `VALIDATION_ERROR`，避免被静默吞掉。
- **不支持的版本被明确拒绝**：
  - `/api/v3/...` → `400 VERSION_NOT_SUPPORTED`，`details` 含 `requested_version / supported_versions / latest_version`；它与 `404 NOT_FOUND`（资源不存在）可区分。
  - `/api/vx/...`（版本段非数字）→ `400 VALIDATION_ERROR`。

---

## 4. 幂等与并发语义

### 4.1 写操作幂等（Idempotency-Key）

- 在写请求上携带请求头 `Idempotency-Key: <客户端生成的唯一键>` 即启用幂等。
- 服务端以 `(方法, 路径, 请求体)` 计算指纹并与键绑定：

| 情形 | 判定 | 结果 |
| --- | --- | --- |
| 首次请求 | `EXECUTE` | 执行真实副作用，固化 `(状态码, 响应体)` |
| 相同键 + 相同体，重复/并发 | `REPLAY` / `IN_FLIGHT→REPLAY` | **不重复执行**，回放首次的状态码与响应体 |
| 相同键 + 不同请求体 | `CONFLICT` | `409 IDEMPOTENCY_CONFLICT` |
| 首个请求在固化结果前失败 | 释放占位 | 允许并发方/重试方重新获得执行权，不会留下半截结果 |

- 并发同键请求在首个请求 `IN_FLIGHT` 期间会等待其完成，然后得到**与首次完全一致**的结果，而不是 409 或重复写入。
- 不带 `Idempotency-Key` 时退化为普通请求（创建同 id 仍会得到 `409 STATE_CONFLICT`）。
- 可通过 `app.state.store.side_effect_count` 断言真实落库次数（测试中用于证明副作用恰为一次）。

### 4.2 并发修改与乐观锁（revision）

- 每次成功写入都会让资源的 `revision`（v1 中为 `rev`）自增。
- `PUT` 必须携带"我基于哪个 revision 修改"的前提字段：
  - 未携带 → `428 PRECONDITION_REQUIRED`（`details.required_field` 指出字段名）。
  - 携带值与当前 revision 不一致 → `409 STALE_WRITE`，`details` 同时给出 `expected_revision` 与 `actual_revision`，明确表示该更新基于陈旧状态、已被拒绝。
- 多个请求基于同一旧 revision 并发更新时：恰好一个成功（revision 前进一格），其余全部 `STALE_WRITE`，从而避免互相覆盖（lost update）。

---

## 5. 统一错误契约

任何失败都返回如下结构（成功响应为裸资源对象，不含 `error` 包装）：

```json
{
  "error": {
    "code": "STALE_WRITE",
    "message": "资源已被其他请求修改，请基于最新版本重试",
    "details": {"document_id": "doc-1", "expected_revision": 0, "actual_revision": 1},
    "trace_id": "8f1c..."
  }
}
```

| `code` | HTTP | 含义 | 典型触发 |
| --- | --- | --- | --- |
| `VALIDATION_ERROR` | 400 | 参数/格式/契约校验失败 | 缺字段、类型错、非法 JSON、旧版本出现未知字段、版本段非数字 |
| `VERSION_NOT_SUPPORTED` | 400 | 请求了未支持的 API 版本 | `/api/v3/...` |
| `NOT_FOUND` | 404 | 资源不存在 | 读取/更新未知 id |
| `PRECONDITION_REQUIRED` | 428 | 更新缺少并发前提字段 | PUT 未带 `rev`/`revision` |
| `STALE_WRITE` | 409 | 基于陈旧版本的更新被拒 | `expected_revision ≠ actual_revision` |
| `STATE_CONFLICT` | 409 | 资源状态冲突 | 无幂等键重复创建；幂等首请求长时间未完成 |
| `IDEMPOTENCY_CONFLICT` | 409 | 幂等键被不同请求体复用 | 同 key 不同 body |
| `INTERNAL_ERROR` | 500 | 未预期内部异常 | 响应只含稳定文案，堆栈仅入服务端日志 |

设计要点：

- 错误码集合是稳定枚举（`app/errors.py`），调用方可据此分支处理；HTTP 状态码与之一一对应。
- 响应体与日志**绝不包含堆栈、内部文件路径或异常细节**；500 的完整堆栈只写服务端日志。
- 校验错误的 `details.errors` 仅保留 `loc / msg / type`，不回显内部对象结构。

### 追踪标识

- 每个请求分配一个 `trace_id`：优先沿用请求头 `X-Trace-Id`（便于跨系统串联），否则服务端生成 32 位十六进制串。
- `trace_id` 同时出现在：响应头 **`X-Trace-Id`**、错误体 `error.trace_id`、以及该请求所有日志的 `[trace_id=...]` 字段。

---

## 6. 代码结构

```
app/
  tracing.py       # trace_id 生成/透传、ASGI 中间件、日志过滤器与格式化器
  errors.py        # 错误码枚举、AppError、统一错误信封与异常处理器
  idempotency.py   # 基于 asyncio 的幂等状态机（EXECUTE/REPLAY/IN_FLIGHT/CONFLICT）
  store.py         # 内存资源存储 + revision 乐观锁 + 真实副作用计数
  contracts.py     # 版本协商、各版本字段编解码（改名映射/未知字段拒绝）
  routers.py       # v1/v2 路由工厂、幂等写入包装、不支持版本兜底
  app.py           # 应用装配（中间件、处理器、共享存储、路由）
main.py            # create_app() 暴露 ASGI app
tests/             # 并发/版本/错误/内部异常测试 + 实时日志
```

### 扩展新版本（例如 v3）

1. 在 `app/contracts.py` 增加 v3 的字段模型、允许字段集合与（改名）映射；
2. 更新 `SUPPORTED_VERSIONS` / `LATEST_VERSION`；
3. 在 `app/app.py` 注册 `create_router(3, ...)`。

旧版本行为与字段集合保持不变，即满足向后兼容。
