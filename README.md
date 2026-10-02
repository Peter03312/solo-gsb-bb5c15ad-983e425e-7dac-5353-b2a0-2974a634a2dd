# 计量校准证书追溯服务

完全可离线运行的计量校准证书签发、追溯与撤销级联服务。

- **技术栈**：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16（`btree_gist`）
- **运行方式**：Docker Compose 一键启动，无需联网拉取应用依赖（镜像内置）
- **时间约定**：所有时间字段均为带时区的 ISO 8601（如 `2026-09-01T08:00:00+00:00`）

---

## 一、快速启动

```bash
# 构建镜像并启动（应用 + PostgreSQL）
docker compose up --build -d

# 查看日志
docker compose logs -f app

# 停止
docker compose down

# 停止并清空数据库数据卷（彻底重置）
docker compose down -v
```

启动后：

| 用途 | 地址 |
| --- | --- |
| 服务基地址 | <http://localhost:8080> |
| 健康检查 | <http://localhost:8080/health> |
| 交互式 API 文档（Swagger UI） | <http://localhost:8080/docs> |
| OpenAPI 规范 | <http://localhost:8080/openapi.json> |
| PostgreSQL（仅绑定宿主机回环地址） | `127.0.0.1:5432` |

首次启动时数据库会自动完成：创建 `btree_gist` 扩展、建表与约束、安装不可改写审计触发器。
该初始化幂等，重复启动不会破坏已有数据。

---

## 二、配置

所有配置通过环境变量注入，可在项目根目录放一份 `.env`（参考 `.env.example`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_PORT` | `8080` | 宿主机映射端口（容器内固定监听 8080） |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `calib` / `calib` / `calibration` | 数据库账号与库名（仅数据卷首次初始化时生效） |
| `DATABASE_URL` | 已在 compose 中按上面账号自动拼好 | 应用连接串 |
| `MAX_CHAIN_DEPTH` | `100` | 追溯链允许的最大深度 |
| `TX_MAX_ATTEMPTS` | `8` | 并发冲突（可串行化失败/死锁）时的事务重试次数 |

生产环境请务必修改 `POSTGRES_PASSWORD` 与 `DATABASE_URL` 中的口令。

---

## 三、领域模型与核心规则

- **设备（Device）**：被校/标准设备，编号唯一。
- **证书（Certificate）**：
  - `REFERENCE`：基准设备合格证书，追溯起点，无上游引用，结论必须为 `PASS`；
  - `NORMAL`：普通证书，必须引用一张标准设备证书作为上游。
- **状态**：`ACTIVE`（有效）/ `REVOKED`（被直接撤销）/ `INVALIDATED`（因上游撤销而级联失效）。

签发普通证书时，系统在一个事务内强制：

1. **引用有效且合格**：直接上游及其整条上游链在校准时刻都处于有效期、状态为 ACTIVE、结论 PASS；
2. **引用链不成环**：递归 CTE 带环检测，成环即拒绝；
3. **链顶为基准**：追溯必须最终到达一张 REFERENCE 合格证书；
4. **同设备有效期不重叠**：同一设备两张 ACTIVE 证书的有效期区间不得相交
   （半开区间 `[valid_from, valid_until)`，端点相接允许）。

撤销与留痕：

- 撤销上游证书时，其**所有下游证书在同一事务中传递级联失效**，接口返回受影响证书清单、
  各自距被撤销证书的引用深度，以及撤销后的完整追溯链；
- **历史签发内容永久可查**：证书的设备、校准时刻、有效期、结论、引用及签发时快照原样保留；
- 证书核心字段由 PostgreSQL 触发器**禁止 UPDATE、证书与审计事件禁止 DELETE**，
  撤销仅会改状态/撤销时间/原因等留痕列。

并发安全（已通过高并发压测验证）：

- 有效期重叠由数据库 **GIST 排他约束**兜底，并发重复签发必有一方收到 409；
- 证书编号唯一约束防止重复编号；
- 签发与撤销等写事务运行在 **SERIALIZABLE** 隔离级别，配合父链行锁与冲突自动重试，
  保证“撤销基准时正在并发签发的下游”要么随撤销一起失效、要么被明确拒绝，绝不漏网。

---

## 四、HTTP API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/devices` | 登记设备 |
| `GET` | `/devices` | 设备列表 |
| `POST` | `/certificates/reference` | 登记基准设备合格证书（追溯起点） |
| `POST` | `/certificates` | 签发普通证书（引用标准设备证书） |
| `GET` | `/certificates` | 查询证书（可按 `device_code` / `status` / `active_at` 过滤） |
| `GET` | `/certificates/{cert_no}` | 查单张证书（任何状态，含签发快照） |
| `GET` | `/certificates/{cert_no}/traceability` | 自下而上追溯到基准，返回完整引用链 |
| `GET` | `/certificates/{cert_no}/downstream` | 查询全部下游（撤销影响面预演） |
| `GET` | `/certificates/{cert_no}/events` | 审计事件（签发/撤销/级联失效） |
| `POST` | `/certificates/{cert_no}/revoke` | 撤销并级联失效全部下游 |

错误响应统一格式：`{"error": "错误码", "message": "中文说明"}`。

常见错误码：

| HTTP | error | 触发场景 |
| --- | --- | --- |
| 409 | `PARENT_NOT_QUALIFIED` | 上游不存在/已撤销/已失效/不合格/校准时刻不在其有效期 |
| 409 | `REFERENCE_CHAIN_CYCLE` | 引用链成环 |
| 409 | `VALIDITY_OVERLAP` / `CONCURRENT_ISSUANCE` | 同设备有效期重叠或并发重复签发 |
| 409 | `DUPLICATE_CERT_NO` | 证书编号已存在 |
| 409 | `CERTIFICATE_NOT_ACTIVE` | 对已撤销/已失效证书重复撤销 |
| 422 | `TIME_RANGE_INVALID` | 有效期起止非法，或校准时刻不在有效期内 |
| 422 | `REFERENCE_ROOT_INVALID` | 基准证书结论非 PASS，或链顶不是基准证书 |
| 404 | `NOT_FOUND` | 设备/证书不存在 |

---

## 五、请求示例（curl）

### 1. 登记设备

```bash
curl -X POST http://localhost:8080/devices \
  -H 'Content-Type: application/json' \
  -d '{"code":"STD-01","name":"一等标准压力计"}'

curl -X POST http://localhost:8080/devices \
  -H 'Content-Type: application/json' \
  -d '{"code":"DUT-11","name":"现场压力表 11 号"}'
```

### 2. 登记基准设备合格证书（追溯起点）

```bash
curl -X POST http://localhost:8080/certificates/reference \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "REF-2026-001",
    "device_code": "STD-01",
    "calibrated_at": "2026-01-10T08:00:00+00:00",
    "valid_from":   "2026-01-10T00:00:00+00:00",
    "valid_until":  "2027-01-10T00:00:00+00:00",
    "conclusion": "PASS"
  }'
```

### 3. 签发普通证书（引用标准设备证书）

```bash
curl -X POST http://localhost:8080/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "C-2026-002",
    "device_code": "DUT-11",
    "calibrated_at": "2026-09-01T08:00:00+00:00",
    "valid_from":   "2026-09-01T00:00:00+00:00",
    "valid_until":  "2027-09-01T00:00:00+00:00",
    "conclusion": "PASS",
    "parent_cert_no": "REF-2026-001"
  }'
```

若校准时刻（如 `2027-02-01`）已超出基准有效期，返回：

```json
{
  "error": "PARENT_NOT_QUALIFIED",
  "message": "引用的标准证书 REF-2026-001 在校准时刻 2027-02-01T08:00:00+00:00 不在有效期内（有效期至 2027-01-10T00:00:00+00:00）"
}
```

### 4. 追溯查询（自下而上到基准）

```bash
curl http://localhost:8080/certificates/C-2026-002/traceability
```

### 5. 查询撤销影响面（全部下游）

```bash
curl http://localhost:8080/certificates/REF-2026-001/downstream
```

### 6. 撤销上游证书（所有下游级联失效）

```bash
curl -X POST http://localhost:8080/certificates/REF-2026-001/revoke \
  -H 'Content-Type: application/json' \
  -d '{"reason": "基准器超差召回，证书作废"}'
```

返回中 `affected` 列出每一张被级联失效的下游证书及引用深度（`depth`），
`traceability` 给出撤销后的链。下游证书状态变为 `INVALIDATED`，
`invalidated_by_cert_no` 指向被撤销证书。

### 7. 撤销后：历史可查、但不能再作依据

```bash
# 历史签发内容（含快照）仍可查
curl http://localhost:8080/certificates/C-2026-002
curl http://localhost:8080/certificates/C-2026-002/events

# 再引用这张已失效证书签发 -> 409 PARENT_NOT_QUALIFIED
```

### 8. 按条件查询

```bash
# 某设备的所有证书
curl "http://localhost:8080/certificates?device_code=DUT-11"

# 某时刻处于有效期内的有效证书
curl "http://localhost:8080/certificates?status=ACTIVE&active_at=2026-10-01T00:00:00%2B00:00"
```

---

## 六、一键端到端演示脚本

服务启动后运行（覆盖：登记基准 → 逐级签发 → 各类非法引用/时间冲突/重复编号报错
→ 并发重复签发 → 追溯 → 撤销级联 → 历史可查 → 失效后拒签）：

```bash
scripts/demo.sh                 # 默认 http://localhost:8080
scripts/demo.sh http://127.0.0.1:9090   # 自定义地址
```

---

## 七、本地（不用 Docker）开发运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 准备一个 PostgreSQL 16，并启用扩展
#   CREATE EXTENSION IF NOT EXISTS btree_gist;

export DATABASE_URL='postgresql+psycopg://calib:calib@localhost:5432/calibration'
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

应用启动时会自动建表、建约束与触发器，无需手动迁移。

---

## 八、项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由与 HTTP 层
│   ├── services.py      # 签发校验、递归追溯、撤销级联、SERIALIZABLE 事务与重试
│   ├── models.py        # ORM 模型、CHECK 约束、GIST 排他约束
│   ├── schemas.py       # 请求/响应模型
│   ├── serialization.py # ORM -> JSON（批量预加载，避免脱离会话后懒加载）
│   ├── database.py      # 引擎、会话、幂等初始化与审计触发器
│   ├── errors.py        # 统一领域错误（错误码 + HTTP 状态）
│   └── config.py        # 环境变量配置
├── db/initdb.d/         # PostgreSQL 首启自动执行（btree_gist 扩展）
├── scripts/demo.sh      # 端到端演示脚本
├── Dockerfile
├── docker-compose.yaml
└── requirements.txt
```
