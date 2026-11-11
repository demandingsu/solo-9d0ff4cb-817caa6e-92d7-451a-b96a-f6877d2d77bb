# 隔离货物召回取件预约系统（本地离线示例）

食品仓库质控员为已隔离货物向生产商预约取件的双站点示例。全部代码仅使用
**Python 3.11 标准库**，无第三方依赖，构建与运行均不需要联网。

## 目录结构

```
.
├── manufacturer/          生产商示例站
│   ├── server.py          召回详情页、预约表单、按交接单号幂等受理、受理查询、故障场景注入
│   └── Dockerfile
├── warehouse/             仓库质控端（取件预约入口）
│   ├── server.py          状态机 + SQLite 持久化 + 并发锁 + 后台核对器
│   ├── recall_parser.py   从召回详情页发现预约表单与受理查询入口
│   ├── httpclient.py      出站 HTTP（urllib），区分传输中断与业务响应
│   └── Dockerfile
├── tests/test_e2e.py      11 个端到端用例 / 54 项断言
├── docker-compose.yml     一键启动（含健康检查、数据卷、容器内地址改写）
└── run_local.sh           无 Docker 时本地直接运行
```

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
docker compose up --build
```

- 质控工作台：<http://localhost:8080>
- 生产商示例站：<http://localhost:8081>
- 数据保存在命名卷 `mfg-data` / `wh-data`，容器重启不丢失。

> 离线主机：先在有网环境 `docker compose pull && docker compose build`
> （仅需 `python:3.11-slim` 基础镜像，不装任何 pip 包），再把目录/镜像带过去。

### 方式二：本地 Python（无需 Docker）

```bash
./run_local.sh
# 或分别：
PORT=8081 python3 -I manufacturer/server.py
PORT=8080 python3 -I warehouse/server.py
```

### 运行测试

```bash
python3 -I tests/test_e2e.py
```

## 业务规则与状态机

质控端每条预约记录按交接单号唯一，状态流转如下：

| 状态 | 含义 | 允许的动作 |
| --- | --- | --- |
| `SUBMITTING` | 提交中（进程崩溃后重启自动转为待确认） | — |
| `PENDING_CONFIRMATION` | 待确认：回执中断/传输失败/查询失败，结果不明 | 仅可“立即核对受理结果”，**禁止重复提交** |
| `CONFIRMED` | 已确认，拿到生产商预约号 | 无（不可重复下单） |
| `NOT_ACCEPTED` | 生产商**明确**未受理该交接单号 | 可“重新提交预约” |

关键保证：

1. **同号至多下单一次**：`handover_no` 数据库 UNIQUE 约束 + 进程内串行锁，
   同号并发点击只有第一个请求提交，其余直接读取既有记录（测试用例 8 验证 10 并发仅 1 次 POST）。
2. **结果不明绝不二次提交**：502/空响应/连接断开/超时一律置待确认，
   只能通过受理查询接口按交接单号核对。
3. **查询三分支严格区分**：
   - 查到受理（`accepted:true` + 预约号）→ 已确认，展示**原预约号**；
   - 明确未受理（HTTP 200 + `accepted:false`）→ 才允许重试；
   - 查询失败（5xx/超时/语义不明）→ 保持待确认，带退避后再查，不提交。
4. **重启恢复**：启动时把残留 `SUBMITTING` 改为待确认，后台核对器
   （默认每 12 秒，退避 10 秒）自动核对所有待确认/提交中交接单。
5. 页面展示**预约状态、预约号、公告来源**（详情页标题与可点击 URL），
   并提供事件日志（提交、回执中断、查询、确认全过程留痕）。

## 表单发现流程（不硬编码生产商接口）

质控端不预设生产商接口地址，每次下单先抓取用户填写的召回详情 URL：

- 用标准库 `html.parser` 找到含 `handover_no` 字段的 `<form>`，取 `action`
  与隐藏字段（如 `announcement_id`）；
- 受理查询入口通过页面 `<link rel="appointment-lookup" href="...">`
  （并兼容表单 `data-lookup` 属性）发现；
- 两个 URL 随记录持久化，重启后核对无需重新解析。

## 示例站故障场景（页面“故障场景控制台”一键切换）

在 <http://localhost:8081> 首页点击按钮，或 `POST /admin/scenario`：

| 场景 | 行为 | 对应质控端表现 |
| --- | --- | --- |
| `receipt_interrupt_once` | **已受理但回执中断**（502 空响应，或设 `MFG_INTERRUPT_STYLE=connection_drop` 直接断连），仅下一次新单生效后自动恢复 | 待确认 → 自动核对出原预约号，无重复预约 |
| `receipt_interrupt_timed` | 60 秒窗口内新单受理后回执中断 | 同上，持续窗口 |
| `query_failure_timed` | 受理查询接口 503 | 保持待确认且不再提交，恢复后自动确认 |
| `booking_outage_timed` | 预约接口 503 且**不落库** | 核对为明确未受理后允许重试 |

生产商侧按交接单号 UNIQUE 幂等：同号再次提交会返回既有预约号并标记 `duplicate:true`。
`/admin/appointments` 可查看所有已受理预约，`/admin/metrics` 可核对接口被调用次数。

## 手动演练步骤

1. 打开 <http://localhost:8081>，点击“模拟：受理成功但回执中断”；
2. 打开 <http://localhost:8080>，填写：
   - 召回详情 URL：`http://localhost:8081/recall/recall-2026-001`
   - 交接单号：`HO-DEMO-01`，箱数：`5`，取件日期任选；
3. 提交后记录显示“待确认”；约十几秒内自动变为“已确认”并出现预约号；
4. 回到生产商站 `/admin/appointments` 可看到只有一条受理记录；
5. 再用同一交接单号提交，工作台提示“已存在，未重复下单”。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` / `HOST` | 8080/8081、`0.0.0.0` | 监听端口/地址 |
| `WH_DB_PATH` / `MFG_DB_PATH` | 各自 `data/` 下 | SQLite 文件路径 |
| `RECONCILE_INTERVAL` | 12 | 后台核对周期（秒） |
| `RECONCILE_BACKOFF` | 10 | 查询失败后再次核对的最小间隔（秒） |
| `HTTP_TIMEOUT` | 8 | 出站 HTTP 超时（秒） |
| `URL_MAP_FROM` / `URL_MAP_TO` | 空 | 出站地址改写（Compose 中将 `localhost:8081` 改写为 `http://manufacturer:8081`） |
| `MFG_INTERRUPT_STYLE` | `empty_502` | 回执中断方式：`empty_502` 或 `connection_drop` |
