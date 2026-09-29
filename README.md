# FastAPI 请求级可观测性与健康检查

在原有 `GET /` 接口不变的前提下，补齐了请求标识、结构化日志、请求指标与存活/就绪检查。

## 请求标识（Request ID）

- 请求头 `X-Request-ID` 为入口与出口：上游传入的**合法**标识会被原样沿用，并写回响应头 `X-Request-ID`。
- 合法规则：非空、长度 ≤ `APP_REQUEST_ID_MAX_LENGTH`（默认 128）、字符集仅限 `[A-Za-z0-9._-]`。
- 缺失、超长、含非法字符或非 ASCII 字节时，按固定规则生成新标识：`req-<uuid4hex>`，**不会让请求失败**。
- 标识存放在 `contextvars` 中，并发请求之间互不串线；所有结构化日志自动携带 `request_id` 字段。
- 未捕获异常的 500 响应由用户中间件（而非最外层 ServerErrorMiddleware）生成，因此错误响应同样带有请求标识头与日志上下文。

## 日志

JSON 单行输出到 stdout，字段：`ts / level / logger / service / request_id / msg`（异常时附 `exc`）。

## 指标

`GET /metrics` 暴露 Prometheus 文本格式，可直接被 Prometheus 抓取：

- `http_requests_total{method,path,status}`：请求计数。
- `http_request_duration_ms{method,path,status}`：耗时直方图（毫秒），桶边界由 `APP_METRICS_BUCKET_MS` 配置。

标签归并策略（保证标签规模有界）：

- `path` 只取**路由模板**（如 `/items/{item_id}`），不含具体参数值；
- 未匹配任何路由的路径（如 404）归并为 `__unmatched__`；
- 路由模板数量超过 `APP_METRICS_MAX_PATH_LABELS`（默认 200）后，新模板归并为 `__overflow__`；
- 因此 path 标签上界为 `max_path_labels + 2`。

计数与直方图由单锁保护，并发下不丢不重；中间件在 `finally` 中记录，下游异常也计为 500。

## 健康检查

- `GET /healthz`：存活检查。只判断本进程能否响应，永远返回 200 `{"status": "alive"}`，与依赖状态无关。
- `GET /readyz`：就绪检查。所有依赖可用才返回 200 `{"status": "ready"}`；依赖异常时返回 503 `{"status": "not_ready"}`，并在 `dependencies[].reason` 中给出**可区分、可枚举**的原因：`ok / unavailable / timeout / error`。依赖检查内部异常被捕获归类为 `error`，不外抛、不拖垮进程，响应只含 `name/ok/reason` 固定字段，不泄露内部细节。

## 异常归类

未捕获异常按类型归类为可区分的 `error` 字段，对外统一返回、不泄露内部细节：

| 类别 | 触发 |
|---|---|
| `validation_error` | 请求参数校验失败（422） |
| `client_error` | 4xx HTTP 异常 |
| `timeout` | `TimeoutError` / `asyncio.TimeoutError` |
| `dependency_error` | `ConnectionError` 及其子类 |
| `unexpected_error` | 其余未捕获异常（500，`detail` 固定为 `internal error`） |

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP_SERVICE_NAME` | `fastapi-telemetry` | 服务名（日志字段） |
| `APP_REQUEST_ID_HEADER` | `X-Request-ID` | 请求标识头名 |
| `APP_REQUEST_ID_MAX_LENGTH` | `128` | 上游标识最大长度 |
| `APP_METRICS_MAX_PATH_LABELS` | `200` | 路径标签上限 |
| `APP_METRICS_BUCKET_MS` | `5,10,25,50,100,250,500,1000,2500,5000` | 耗时直方图桶边界（毫秒） |
| `APP_DEPENDENCY_TIMEOUT_SECONDS` | `0.5` | 依赖检查超时 |
| `APP_DEPENDENCY_OK` | `true` | 本地模拟依赖开关（`false` 时就绪检查降级） |

## 本地验证

```bash
uv sync
uv run pytest tests/ -q          # 运行测试（日志含触发输入与判定依据）

uv run uvicorn main:app --port 8000 &
curl -i localhost:8000/                                # 响应头含生成的 X-Request-ID
curl -i -H 'X-Request-ID: up-123' localhost:8000/      # 合法上游标识被沿用
curl -i localhost:8000/items/42                        # 路径参数路由
curl -i localhost:8000/boom                            # 500 + error=unexpected_error
curl localhost:8000/metrics                            # Prometheus 文本格式
curl localhost:8000/healthz                            # 存活
curl localhost:8000/readyz                             # 就绪
APP_DEPENDENCY_OK=false uv run uvicorn main:app --port 8001
curl -i localhost:8001/readyz                          # 503 not_ready, reason=unavailable
```
