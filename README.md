# FastAPI 请求级可观测性与健康检查

在不改动既有对外接口（`GET /` 仍返回 `{"Hello": "World"}`）的前提下，为服务补齐
**请求标识、结构化日志、Prometheus 指标、存活/就绪探针、异常归类** 五件事，并保证
日志、指标、健康状态对同一个请求给出**一致、可解释**的结论。

## 目录结构

```
app/
  config.py            # 配置项（环境变量，带默认值）
  request_context.py   # 请求标识的校验/生成/ContextVar 隔离
  logging_setup.py     # JSON 行结构化日志 + 请求标识注入
  metrics.py           # 计数器/直方图、标签归并、Prometheus 渲染（线程安全）
  health.py            # 存活/就绪探针 + 可注入故障的下游依赖
  errors.py            # BusinessError / DependencyError 与归类口径
  middleware.py        # 请求标识中间件 + 异常归类处理器
  routes.py            # 业务路由（GET / 行为不变 + 演示端点）
  dependencies.py      # 可注入依赖
  factory.py           # 应用组装
main.py                # uvicorn 入口（app = create_app()）
tests/                 # 23 个单测，全部只走内存 ASGI，无需外部服务/凭据
```

---

## 1. 请求标识（Request ID）

### 传递与生成规则

- 请求头名默认 `X-Request-ID`（可配置）。每个请求在**入口**解析一次：
  1. **上游合法 → 沿用**。合法条件：非空、长度 ≤ `REQUEST_ID_MAX_LENGTH`（默认 128）、
     仅含 `A-Z a-z 0-9 - _ . ~`（RFC 3986 unreserved）。显式拒绝空白符与控制字符，
     从字符集层面杜绝日志伪造与响应头 CRLF 注入。
  2. **缺失或非法 → 生成**：`req-` + 32 位 UUID4 十六进制，固定 36 字符，例如
     `req-3f1c…`。非法/超长**不会让请求失败**，也不会回显或落日志原始脏值，只记一条
     `inbound request id rejected` 警告（含长度，不含原值）。
- 结论同时出现在两处：
  - **响应头** `X-Request-ID`（无论沿用还是新生成都会带上）；
  - **每条结构化日志** 的 `request_id` 字段（业务日志、访问日志、错误日志共用）。
- 业务代码通过 `app.request_context.get_request_id()` 随时读取当前请求标识。

### 并发隔离

- 标识存于 `contextvars.ContextVar`。每个 ASGI 请求运行在独立复制的上下文中，
  并发请求互不串号；同步（线程池）端点同样安全（Starlette/anyio 会复制上下文）。
  `GET /whoami/sync` 专门验证线程池场景。

---

## 2. 结构化日志

- 每行一条 JSON，输出到 stderr，字段固定：
  `ts / level / logger / msg / request_id`，其余 `extra` 字段平铺同层。
- 每个请求在出口写一条访问日志：
  `method, path, route, status_code, outcome, duration_s, id_reused`。
- 请求标识在日志真正输出的那一刻从当前上下文读取，而非进入中间件时快照，
  因此并发共用 handler 也不会串号。
- 错误日志带 `outcome`、`error_type`，异常堆栈在 `exc` 字段；内部细节只进日志。

示例：

```json
{"ts":"2026-09-29T…Z","level":"INFO","logger":"app.access","msg":"request completed",
 "request_id":"req-…","method":"GET","path":"/items/42","route":"/items/{item_id}",
 "status_code":200,"outcome":"success","duration_s":0.0012,"id_reused":false}
```

---

## 3. 指标（Prometheus 文本格式）

`GET /metrics` 返回 `text/plain; version=0.0.4`，可被 Prometheus 直接抓取。

| 指标 | 类型 | 标签 | 含义 |
|---|---|---|---|
| `http_requests_total` | counter | `service, method, route, status, outcome` | 完成请求计数 |
| `http_request_duration_seconds` | histogram | `service, method, route`（含 `le`） | 请求耗时分布，桶：`.005,.01,.025,.05,.1,.25,.5,1,2.5,5` 秒，附 `_sum`/`_count` |

### 标签口径

- **method**：HTTP 方法。
- **route**：命中已注册路由时用**路由模板**，路径参数天然归并
  （`/items/1`、`/items/2` → `/items/{item_id}`）；未命中任何路由的请求统一记为
  `__unmatched__`，因此扫漏洞/随机高基数路径不会制造无限标签。
- **status**：具体 HTTP 状态码（有限集合 100–599），越界值归 `other`。
- **outcome（失败归类，六选一）**：
  - `success`（2xx/3xx）
  - `http_4xx` / `http_5xx`（框架层 HTTP 错误，如 404、422）
  - `business_error`（业务规则拒绝）
  - `dependency_error`（下游依赖失败/超时/就绪探针不就绪）
  - `unexpected_error`（未预期异常，统一 500）

### 基数保护与并发正确性

- 不同 `(method, route)` 序列数有硬上限 `CARDINALITY_LIMIT=200`；触顶后新组合整体并入
  `route="__other__"`，标签集合从此有上界，长时间运行不会无限增长。
- 所有读/写在同一把线程锁内完成；一次请求的计数器与直方图严格各 +1，
  并发下不丢、不重复（`test_concurrent_observations_are_counted_exactly_once` 压测 600 请求）。

> 说明：`/metrics` 自身也会被统计；某次抓取的快照不包含“这一次抓取本身”，
> 它出现在下一次快照里。

---

## 4. 健康检查

| 探针 | 路径 | 语义 | 状态码 |
|---|---|---|---|
| 存活 liveness | `/health/live` | 只判断**本进程**事件循环是否能响应；依赖故障**不影响**存活，避免依赖抖动触发整进程被重启 | 恒 `200` |
| 就绪 readiness | `/health/ready` | 检查下游依赖；任一依赖不可用即不就绪，并给出可区分原因 | `200` / `503` |

就绪响应（不泄露内部细节，只给固定枚举原因）：

```json
{ "status": "ok",          "checks": { "downstream": "up" } }
{ "status": "unavailable", "checks": { "downstream": "dependency_unavailable" } }
```

可区分的 `checks.downstream` 原因：

- `up`：依赖正常；
- `dependency_unavailable`：依赖明确不可用；
- `dependency_timeout`：在 `DEPENDENCY_PROBE_TIMEOUT` 内未响应（`asyncio.wait_for` 兜底，
  依赖永久挂起也不会拖垮进程）；
- `dependency_error`：探针内部抛异常（被吞掉，堆栈只进日志）。

不就绪时该探针在指标/日志中归类为 `dependency_error`，与服务自身 5xx 明确区分。

---

## 5. 异常归类与对外错误体

业务里抛出两类受控异常（`app/errors.py`），其余异常自动兜底：

| 异常 | outcome | HTTP | 对外 `error.type` | 内部细节 |
|---|---|---|---|---|
| `BusinessError` | `business_error` | 4xx（默认 400，可指定） | `business_error` | 业务消息可回显 |
| `DependencyError` | `dependency_error` | 503 | `dependency_error` | `internal_detail` 只进日志 |
| 任意未预期 `Exception` | `unexpected_error` | 500 | `unexpected_error` | 固定文案 `internal server error`，不回显堆栈/异常名 |

错误响应统一结构：`{"error": {"type": <outcome>, "message": <受控文案>}}`，
并同样带 `X-Request-ID`。HTTP 层错误（404/422 等）归 `http_4xx`/`http_5xx`。
同一请求的**响应体 type、访问日志 outcome、指标 outcome 三者一致**。

演示端点：`/items/missing`（业务异常）、`/items/boom`（依赖异常）、
`/items/crash`（未预期异常）。

---

## 6. 配置项

均通过环境变量配置，缺省/非法值回落到默认值（见 `app/config.py`）：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `SERVICE_NAME` | `fastapi-telemetry-demo` | 服务名，用于日志与 `service` 标签 |
| `REQUEST_ID_HEADER` | `X-Request-ID` | 请求标识的请求/响应头名称 |
| `REQUEST_ID_MAX_LENGTH` | `128` | 沿用上游标识允许的最大字符数 |
| `METRICS_PATH` | `/metrics` | 指标端点路径 |
| `HEALTH_LIVENESS_PATH` | `/health/live` | 存活探针路径 |
| `HEALTH_READINESS_PATH` | `/health/ready` | 就绪探针路径 |
| `DEPENDENCY_PROBE_TIMEOUT` | `0.5` | 就绪探针等待依赖的最长秒数 |

---

## 7. 本地验证

```bash
# 安装依赖（Python >= 3.14）
uv sync

# 跑全部单测（无需外部服务/端口/凭据；用例会打印触发输入与判定依据）
uv run pytest -q
uv run pytest -s -q tests/test_request_id.py   # 查看每个用例的输入/判定打印

# 启动服务
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

手动验证（另开终端）：

```bash
# 请求标识：沿用
curl -si -H 'X-Request-ID: my-trace-001' http://127.0.0.1:8000/whoami
# 请求标识：缺失自动生成 / 非法自动替换（请求仍 200）
curl -si http://127.0.0.1:8000/whoami
curl -si -H 'X-Request-ID: bad value' http://127.0.0.1:8000/whoami

# 健康探针
curl -s -w ' [%{http_code}]\n' http://127.0.0.1:8000/health/live
curl -s -w ' [%{http_code}]\n' http://127.0.0.1:8000/health/ready

# 三类异常归类（观察 error.type 与 x-request-id）
curl -si http://127.0.0.1:8000/items/missing   # business_error / 404
curl -si http://127.0.0.1:8000/items/boom      # dependency_error / 503
curl -si http://127.0.0.1:8000/items/crash     # unexpected_error / 500

# 指标（路径参数模板化、未知路径 __unmatched__、outcome 标签）
curl -s http://127.0.0.1:8000/metrics
```

服务端 stderr 可直接看到与请求一一对应的 JSON 日志（含 `request_id`、`outcome`、耗时）。

### 测试覆盖（23 项）

- `test_request_id.py`：合法沿用、缺失生成、非法替换、超长容错、**80 并发请求 ID 隔离**；
- `test_metrics.py`：分组计数、路径参数模板化、高基数未知路径归并、基数硬上限、
  Prometheus 文本格式、**并发计数精确一次**；
- `test_health.py`：依赖各种故障下存活恒真、就绪随状态切换、三类失败原因可区分、
  不泄露内部细节、探针不拖垮进程且可恢复；
- `test_exception_classification.py`：业务/依赖/未预期三类异常在响应、日志、指标
  三处的归类一致且互不串用。
