# 潮位通航窗口 API（FastAPI + SciPy + SQLite）

根据按 UTC 递增的潮位样本，使用 **PCHIP（单调保形三次 Hermite）插值** 求出潮高
不低于安全水深（`吃水 + 最小富余`）的全部连续通航窗口；阈值交点用 Brent 法精确
求根并**保守取整到秒**。相邻样本间隔超过 `max_gap_seconds` 的区段**禁止插值**，
作为 `unknown` 返回，通航窗口不会跨越该区段。

可选的**航程占用约束**：创建任务时传入 `transit_duration_seconds`（航程占用秒数）
与 `maintenance_blocks`（维护封航区间，UTC 起止）后，结果额外包含
`departure_windows` —— 仅保留**从出发到结束全程落在同一安全通航窗口**、且不与任何
维护封航区间相交的出发时段（占用区间按 `[出发时刻, 出发时刻+航程)` 处理）。

计算以**异步任务**方式执行：先创建任务拿到 ID，再按 ID 轮询结果；任务状态与
结果持久化在 SQLite 中（容器启动时自动建库建表）。

除单船任务（`POST /tasks`）外，还提供**船队协同**任务（`POST /fleet-tasks`）：
一组船舶共享同一份潮位样本、查询区间、最大采样间隔与封航区间，各自给出名称、
吃水、富余水深与航程；后台先分别计算每艘船的安全/出发窗口，再求**全队都能在
同一时刻出发并完成航程**的公共出发区间，并标明是哪些船把某段时间排除在了公共
区间之外。未知采样段与封航按**全队约束**处理：任何船都不能跨越。

## 一条命令启动

前置条件：已安装 Docker 与 Docker Compose 插件（v2）。

```bash
docker compose up --build -d
```

启动后访问：

- Swagger 交互文档：<http://localhost:8000/docs>
- ReDoc 文档：<http://localhost:8000/redoc>
- OpenAPI 描述：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>

停止 / 查看日志：

```bash
docker compose down        # 停止（数据保留在命名卷 tide-data 中）
docker compose logs -f
```

## 必要配置

| 配置 | 位置 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `TIDE_DB_PATH` | 环境变量（compose 中） | `/data/tide.db` | SQLite 数据库文件路径，启动时自动创建目录、文件与表结构 |
| 端口映射 | `docker-compose.yml` | `8000:8000` | 左侧为宿主机端口，如被占用可改为 `8080:8000` |
| 数据卷 | `docker-compose.yml` | `tide-data:/data` | 数据库持久化卷，`docker compose down` 后数据仍保留 |

本地（非容器）运行：

```bash
pip install -r requirements.txt
TIDE_DB_PATH=./data/tide.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

### 1. 创建计算任务

`POST /tasks` → `201 Created`，立即返回任务 ID（`status` 初始为 `pending`），
计算在后台进行。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `samples` | 数组（≥2） | 潮位样本，按 UTC 时间**严格递增**：`{"time", "height_m"}` |
| `query_start` / `query_end` | datetime | 查询起止时间（ISO-8601，建议带 `Z`；无时区按 UTC 处理） |
| `draft_m` | number ≥ 0 | 船舶吃水（米） |
| `clearance_m` | number ≥ 0 | 船底最小富余水深（米） |
| `max_gap_seconds` | number > 0 | 相邻样本最大允许间隔（秒），超过则该区段禁止插值 |
| `transit_duration_seconds` | number > 0，可选 | 航程占用时长（秒）；提供后结果附带 `departure_windows` |
| `maintenance_blocks` | 数组，可选 | 维护封航区间 `[{"start", "end"}]`（UTC 起止）；需配合 `transit_duration_seconds` 使用，计算前会裁剪到查询范围并合并重叠/首尾相接的区间 |

> 潮高、吃水、富余必须使用**同一垂直基准面**，单位统一为米。
> 安全水深阈值 = `draft_m + clearance_m`。
> 不传 `transit_duration_seconds` / `maintenance_blocks` 时，行为与之前完全一致
> （`departure_windows` 为 `null`）。

```bash
curl -X POST http://localhost:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "samples": [
      {"time": "2026-09-21T00:00:00Z", "height_m": 1.0},
      {"time": "2026-09-21T01:00:00Z", "height_m": 3.0},
      {"time": "2026-09-21T02:00:00Z", "height_m": 6.0},
      {"time": "2026-09-21T03:00:00Z", "height_m": 8.0},
      {"time": "2026-09-21T04:00:00Z", "height_m": 8.0},
      {"time": "2026-09-21T05:00:00Z", "height_m": 6.0},
      {"time": "2026-09-21T06:00:00Z", "height_m": 3.0},
      {"time": "2026-09-21T07:00:00Z", "height_m": 1.0}
    ],
    "query_start": "2026-09-21T00:00:00Z",
    "query_end":   "2026-09-21T07:00:00Z",
    "draft_m": 4.0,
    "clearance_m": 1.0,
    "max_gap_seconds": 3700,
    "transit_duration_seconds": 1800,
    "maintenance_blocks": [
      {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
    ]
  }'
```

### 2. 按 ID 查询任务

`GET /tasks/{task_id}` → `200`（不存在返回 `404`）。

`status`：`pending`（计算中）/ `done`（结果见 `result`）/ `failed`（原因见 `error`）。

`result.windows[]`（**全部**连续安全窗口，起止时间为整秒、UTC `Z` 结尾）：

| 字段 | 说明 |
| --- | --- |
| `start` / `end` | 窗口起止（保守取整：交点向上/向下取整秒，保证窗口内任意时刻满足水深） |
| `start_height_m` / `end_height_m` | 窗口边界处插值潮高（米） |
| `min_height_m` | 窗口内插值潮高最小值（米，由内部样本点与边界取小者） |
| `duration_seconds` | 窗口时长（秒） |

`result.unknown[]`（无法判定的区段，窗口不会跨越）：

| `reason` | 含义 |
| --- | --- |
| `sample_gap_exceeds_max_gap` | 相邻样本间隔超过 `max_gap_seconds`，按要求禁止插值 |
| `no_sample_coverage` | 查询范围超出全部样本覆盖区间（不做外推） |

`result.departure_windows[]`（仅当请求提供 `transit_duration_seconds` 时返回，否则为 `null`）：

| 字段 | 说明 |
| --- | --- |
| `start` / `end` | 可出发时段起止（整秒、UTC `Z` 结尾）：在该时段内任意时刻 `t` 出发，占用区间 `[t, t+航程)` 全程落在**同一**安全通航窗口内，且不与任何维护封航区间或 `unknown` 区段相交 |
| `duration_seconds` | 可出发时段时长（秒） |

> 边界规则：占用区间为半开区间 `[t, t+航程)`，因此在封航开始时刻减去航程的
> 整点、或封航结束时刻整点出发是**允许**的（占用区间与封航区间仅端点相接、
> 不相交）。封航区间先裁剪到查询范围、合并重叠或首尾相接的区间后，再整体
> 向前平移航程时长，从各安全窗口的候选出发区间中扣除。

示例响应（上面请求的真实结果，节选）：

```json
{
  "task_id": "51341849cf6c44cc96b48dc19fb0f4f0",
  "status": "done",
  "created_at": "2026-09-21T13:50:03.600093Z",
  "result": {
    "threshold_m": 5.0,
    "max_gap_seconds": 3700.0,
    "windows": [
      {
        "start": "2026-09-21T01:39:11Z",
        "end": "2026-09-21T05:20:49Z",
        "start_height_m": 5.001,
        "end_height_m": 5.001,
        "min_height_m": 5.001,
        "duration_seconds": 13298
      }
    ],
    "unknown": [],
    "departure_windows": [
      {
        "start": "2026-09-21T01:39:11Z",
        "end": "2026-09-21T02:30:00Z",
        "duration_seconds": 3049
      },
      {
        "start": "2026-09-21T04:00:00Z",
        "end": "2026-09-21T04:50:49Z",
        "duration_seconds": 3049
      }
    ]
  },
  "error": null
}
```

其中安全窗口为 01:39:11–05:20:49；封航 03:00–04:00 向前平移 30 分钟航程后
扣除出发区间 (02:30, 04:00)，故可出发时段被切成两段（02:30 与 04:00 整点
出发均允许），且每段最晚出发时刻都比窗口结束早 30 分钟。

校验失败（如样本未严格递增、吃水为负、封航区间 end 不晚于 start、只传
`maintenance_blocks` 而未传 `transit_duration_seconds` 等）统一返回 `422`
并指明字段与原因。

### 3. 创建船队协同任务

`POST /fleet-tasks` → `201 Created`，同样立即返回任务 ID，后台计算后轮询
`GET /fleet-tasks/{task_id}`（不存在返回 `404`），`status` 语义与单船任务一致，
结果持久化在 SQLite 的 `fleet_tasks` 表中。

请求体（全队共享潮位样本/查询区间/最大采样间隔/封航区间，船舶参数逐艘给出）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `samples` | 数组（≥2） | 全队**共享**的潮位样本，按 UTC 严格递增 |
| `query_start` / `query_end` | datetime | 共享查询起止时间 |
| `max_gap_seconds` | number > 0 | 共享最大采样间隔，超过的未知段对全队禁行 |
| `vessels` | 数组（≥1） | 船舶参数：`name`（任务内唯一、非空）、`draft_m`（≥0）、`clearance_m`（≥0）、`transit_duration_seconds`（>0，每艘船各自的航程秒数） |
| `maintenance_blocks` | 数组，可选 | 全队共享的封航区间；裁剪到查询范围并合并重叠/首尾相接区间 |

```bash
curl -X POST http://localhost:8000/fleet-tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "samples": [
      {"time": "2026-09-21T00:00:00Z", "height_m": 1.0},
      {"time": "2026-09-21T01:00:00Z", "height_m": 3.0},
      {"time": "2026-09-21T02:00:00Z", "height_m": 6.0},
      {"time": "2026-09-21T03:00:00Z", "height_m": 8.0},
      {"time": "2026-09-21T04:00:00Z", "height_m": 8.0},
      {"time": "2026-09-21T05:00:00Z", "height_m": 6.0},
      {"time": "2026-09-21T06:00:00Z", "height_m": 3.0},
      {"time": "2026-09-21T07:00:00Z", "height_m": 1.0}
    ],
    "query_start": "2026-09-21T00:00:00Z",
    "query_end":   "2026-09-21T07:00:00Z",
    "max_gap_seconds": 3700,
    "vessels": [
      {"name": "远洋号", "draft_m": 4.0, "clearance_m": 1.0, "transit_duration_seconds": 1800},
      {"name": "浅吃号", "draft_m": 2.0, "clearance_m": 0.5, "transit_duration_seconds": 3600}
    ],
    "maintenance_blocks": [
      {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
    ]
  }'
```

`result` 字段：

| 字段 | 说明 |
| --- | --- |
| `vessels[]` | 每艘船的单独结果：`name`、`threshold_m`、`windows[]`（该船安全窗口）、`departure_windows[]`（该船出发窗口） |
| `unknown[]` | **全队共享**的未知段（与单船含义相同；潮位样本相同，各船未知段完全一致，故只返回一份） |
| `common_departure_windows[]` | **公共出发区间**：区间内任意时刻出发，**所有船**的占用区间 `[t, t+各自航程)` 都全程落在各自同一安全窗口内，且不触碰封航区间与未知段 |
| `exclusions[]` | 被排除出公共集合的区段：`start`/`end`/`duration_seconds` 与 `vessel_names`（**导致该段被排除的船名**——这些船在该段内无法完成航程，其余船可以） |

排除段的语义：

- 只有**部分船**可行的时间段才会出现在 `exclusions` 中，并列出是哪（几）艘船
  受限；例如 A 船的安全窗口比 B 船窄，则 A 窗口之外、B 窗口之内的两段会标记为
  `vessel_names: ["A"]`。
- **所有船都不可行**的时间段（共享未知采样段、共享封航平移后的共同扣除段）不属于
  任何船的候选出发域，**不会**出现在 `exclusions` 中（它不是"被某艘船从公共集合
  中排除"的，而是全队都不能走）；封航与未知段的约束对每艘船都生效。
- 公共窗口与排除段一起，覆盖"至少一艘船能出发"的全部候选域；无公共窗口时
  `common_departure_windows` 为 `[]`，可依据 `exclusions` 判断瓶颈船舶。

校验失败（样本未严格递增、船名重复或为空、`vessels` 为空、航程非正、吃水/富余
为负或非有限、封航区间非法等）统一返回 `422`。

## 算法说明

1. **时间数值化**：所有时间转 POSIX 秒；无时区输入按 UTC 处理。
2. **链分割**：相邻样本间隔 `> max_gap_seconds` 处切分为若干独立样本链。
   每条链单独构造 PCHIP，**任何三次分段都不会跨越该间隙**；孤立样本点
   （链长为 1）不产生有效覆盖。
3. **PCHIP 插值 + 内部极值**：PCHIP 保形（不会超过数据点极值），但单个三次
   分段在非单调数据附近仍可能出现内部极值。因此对每条链用
   `derivative().roots()` 取全部驻点，与样本节点共同构成事件网格——相邻事件
   之间曲线单调，是否跨阈值只需比较两端符号。
4. **阈值求根**：对跨阈值的相邻事件用 `scipy.optimize.brentq`（容差 `1e-6` 秒，
   远高于秒级要求）精确求出交点。
5. **保守取整到秒**：窗口起点 `ceil`、终点 `floor`，舍去不足整秒的窗口，
   保证报告的每一秒潮高都不低于安全水深。
6. **unknown 互补集**：查询区间内未被任何有效链覆盖的部分即为 `unknown`，
   并区分“超间隔（禁止插值）”与“无样本覆盖（不外推）”两种原因；
   窗口严格落在覆盖区内，绝不跨越未知段。
7. **出发窗口（可选）**：提供 `transit_duration_seconds` 时，在每个安全窗口
   `[ws, we]` 内取候选出发区间 `[ws, we - 航程]`（保证占用区间
   `[t, t+航程)` 全程落在同一窗口内）；维护封航区间裁剪到查询范围、合并
   重叠/首尾相接区间后，与 `unknown` 区段一并向前平移航程时长，从候选区间
   中扣除（扣除区间为开区间，端点时刻出发仍允许），最后同样保守取整到秒。
8. **船队协同**：对每艘船（各自的吃水/富余阈值与航程）独立执行步骤 1–7；
   共享样本意味着各船的 `unknown` 完全相同。取所有船出发窗口端点构成事件
   网格扫描：相邻事件之间用"覆盖船集合"恒定"的性质分段——全部船覆盖记为
   **公共出发区间**，部分船覆盖记为**排除段**（附上未覆盖该段的船名），无船
   覆盖（封航/未知段的共同扣除结果）则不属于候选域。同类相邻段合并；端点为
   闭区间（端点时刻允许出发），单独一个可行时刻不会把排除段切成两段。

## 项目结构与测试

```
app/
  main.py            # FastAPI 路由、任务创建/查询、后台计算（单船 + 船队）
  schemas.py         # 请求/响应模型与输入校验
  config.py          # 配置（TIDE_DB_PATH 等）
  db.py              # SQLite 连接与启动建表（tasks / fleet_tasks）
  services/tides.py  # PCHIP 插值、求根、窗口与 unknown 计算
  services/fleet.py  # 船队协同：逐船计算、公共出发窗口与排除段扫描
  tests/             # pytest：算法单测 + 接口端到端测试
Dockerfile           # 依赖安装于构建阶段
docker-compose.yml   # 一条命令启动，含卷持久化与健康检查
requirements.txt
```

运行测试（本地）：

```bash
python -m pytest app/tests -q
```
