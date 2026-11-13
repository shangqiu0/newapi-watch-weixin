> 历史资料提示（2026-11-10）：后续用户已确认所有参与者权限一致，不做多用户/角色平台。当前实现与修订结论请以 README.md、docs/实现说明与验证.md 和 docs/superpowers/specs/2026-11-10-newapi-watch-design.md 为准；以下原始记录保留备查。

# NewAPI 中转平台监控告警 + 企业微信机器人 —— 完整技术方案

> 版本 v2.0 · 2026-11 · 基于 NewAPI (QuantumNous/new-api) + 企业微信智能机器人
> 目标：**零侵入**（外部独立服务监控，NewAPI 不改源码、不重启）监控 NewAPI 渠道状态/日志，告警推送到企业微信群，支持群聊/私聊对话式查询渠道/用户信息、图表统计、每日总结，并支持后期扩展二次开发（优先二开成熟项目）。

## 0. v2.0 变更说明（回应三个新要求）

1. **零侵入确认**：本方案 100% 外部部署。三条数据获取途径（事件 webhook、管理 API 轮询、主动探测）全部从外部访问 NewAPI，监控服务升级/重启完全不影响 NewAPI 中转站运行。已从 NewAPI 源码（`service/channel.go`、`service/user_notify.go`、`service/webhook.go`）核实其自带的通知事件机制。
2. **MCP 只是可选路线之一**：第 11 章给出 5 条完整方案路线对比（纯轮询 / 事件驱动 / 机器人框架二开 / MCP / 混合），MCP 降级为“后期可选能力”，非必需。
3. **二开路线**：第 12 章给出三个可直接二开的项目及改造成本评估。

---

## 1. 需求分解

| # | 需求 | 实现途径 |
|---|------|----------|
| 1 | 监控渠道状态（在线/禁用/异常） | NewAPI 管理 API：`GET /api/channel/`、`GET /api/channel/test/:id`、`GET /api/channel/test` |
| 2 | 监控日志（调用错误、延迟、消耗） | `GET /api/log/`、`GET /api/log/search`、`GET /api/log/stat`、`GET /api/data/` |
| 3 | 告警推送到企业微信群 | 群机器人 Webhook（markdown/图文卡片，20条/分钟） |
| 4 | 群聊 @机器人 查询 | 智能机器人 API 模式（长连接 WebSocket，官方 Python SDK） |
| 5 | 私聊机器人查询 | 智能机器人单聊（同一连接，chattype=single） |
| 6 | 图表统计 | matplotlib/ECharts 生成图片 → 群机器人 image 消息（base64+md5） |
| 7 | 每日总结 | 定时任务（cron）汇总当日日志 → 群推送 markdown + 图表 |
| 8 | 信息查询（用户/额度/令牌） | `GET /api/user/`、`GET /api/token/`、`GET /api/data/self` 等 |
| 9 | 支持扩展二次开发 | 插件式架构 + 命令注册 + 采集器注册（详见第 6 章） |

---

## 2. 调研结论（关键事实，已核实）

### 2.1 NewAPI 侧

- **项目**：QuantumNous/new-api，AGPL-3.0（附加条款要求修改版保留原仓库可见链接；商用需联系官方）。Go + Gin 后端，React 前端，SQLite/MySQL/PostgreSQL 存储。
- **本项目为纯外部部署，不修改 NewAPI 源码 → AGPL 义务不触发，无传染风险。**
- **⚠️ NewAPI 自带事件通知（v2 新核实，本方案的事件数据源）**——管理员在后台配置 webhook 后，以下事件会 POST 到你的服务（`X-Webhook-Signature` HMAC-SHA256 签名可验）：
  - **渠道自动禁用**（`DisableChannel`：错误次数/状态码/关键词触发，调 `UpdateChannelStatus(status=3)` 后发通知）
  - **渠道自动恢复启用**（`EnableChannel`：探测成功且当前为自动禁用态）
  - **渠道测试完成**（批量测试任务结束）
  - **上游模型列表变更**（`NotifyUpstreamModelUpdateWatchers`）
  - 内置通知渠道有：邮件 / webhook / Bark / Gotify（**无企微直推** → 企微推送正是本服务的价值点）
  - 通知默认发给 root 及开启 `ChannelNotifyEnabled` 的管理员
- **监控所需的全部管理 API 都存在**，且均需管理员会话（或带权限 scope 的 token）：
  - 渠道列表 `GET /api/channel/`（支持 `status`、`type`、`p`、`page_size` 过滤）
  - 单渠道测试 `GET /api/channel/test/:id` → 返回 `{"success":bool,"message":str,"time":float,"error_code":...}`
  - 全渠道测试 `GET /api/channel/test` → 异步任务，返回 `task_id`（409 表示已在跑）
  - 更新状态 `POST /api/channel/:id/status`、批量 `POST /api/channel/status/batch`
  - 余额更新 `GET /api/channel/update_balance/:id`
  - 日志 `GET /api/log/`、`GET /api/log/search`、统计 `GET /api/log/stat`、导出 `GET /api/log/export`
  - 数据看板 `GET /api/data/`（按日聚合 quota）、`GET /api/data/users`、`GET /api/data/flow`
  - 用户/令牌管理 `GET /api/user/`、`GET /api/token/` 系列
- **渠道状态常量**（`common/constants.go`）：`1=启用`、`2=手动禁用`、`3=自动禁用`（0=未知，勿当启用用）。
- NewAPI 自身也有插件系统（JS 任务插件 / RelayKit），但**监控告警类需求放在外部独立部署更稳**（NewAPI 升级不影响监控，无需改其源码——已验证 newapi-monitor 项目同样是外部部署思路）。

### 2.2 企业微信侧

**三条推送通道，各有用途，方案中同时使用：**

| 通道 | 用途 | 关键限制 |
|------|------|----------|
| ① 群机器人 Webhook | 告警推送、每日总结、图表图片 | 20 条/分钟；markdown ≤4096B；图片 base64+md5 ≤2MB |
| ② 智能机器人（API 模式·长连接） | 群 @机器人问答、私聊问答、流式回复 | **无需公网 IP**；wss://openws.work.weixin.qq.com；30条/分钟·1000条/小时；回包窗口 24h；需在企微管理后台开启“API 模式” |
| ③ 自建应用 message/send | 定向私推管理员（严重告警） | 需 corpid+corpsecret+agentid；同成员 30条/分·1000条/时；接收人须在应用可见范围 |

- 智能机器人**长连接模式有官方 Python SDK**：`wecom-aibot-python-sdk`（pip 安装，MIT，含 reply_stream 流式回复、模板卡片、主动 send_message、文件 AES 解密）；Node 侧有 `@wecom/aibot-node-sdk`。
- 回调模式（需公网 URL + Token/EncodingAESKey 加解密）是备选；长连接免加解密、免公网，优先。
- API 模式两种模式二选一（长连接/callback 切换会互斥）。
- 群聊只有 @机器人 的消息会触发回调（content 形如 `"@RobotA 查渠道"`）；私聊支持文本/语音(自动转文本)/图片等。
- 需按 `msgid` 去重（网络可能重推）。

### 2.3 现成轮子（可直接参考/借鉴）

| 项目 | 技术 | 与本需求差异 |
|------|------|--------------|
| **li0on3/newapi-monitor** (Python, FastAPI+React, MIT) | 渠道探测/延迟分析/资源监控/**企微告警(群+应用)**/SSO | 功能最接近但无对话式机器人查询，可借鉴其探测与告警设计 |
| yigehaozi/relay-status-monitor (TS, Next.js, MIT) | 余额/延迟/测速/飞书告警 | 飞书而非企微，可借鉴采集分层（轻/重量探测） |
| Kazi6de1b/LLM-Upstream-Monitor (TS) | 余额趋势+TG/SMTP 告警 | 告警渠道不同 |
| Ruanyuxi1337/newapi-operator (Go, MCP) | 全管理面 MCP 化 | 若想让任意 AI 客户端管理 NewAPI 可参考 |

**结论：没有“企微智能机器人对话 + NewAPI 监控”合一的现成方案 → 需自建，但每块都有官方 SDK/文档支撑，工作量可控。**

---

## 3. 总体架构

```
                        ┌─────────────────────────────────────────────┐
                        │            NewAPI 监控服务 (本项目)           │
                        │                                             │
  ┌──────────┐   HTTP   │  ┌─────────────┐    ┌──────────────────┐    │
  │  NewAPI   │◄────────┼──┤  采集层       │───►│   存储 (SQLite)    │    │
  │ 中转平台   │  管理 API│  │ Collector    │    │ channels/logs/   │    │
  └──────────┘          │  │ 定时+事件驱动 │    │ metrics/alerts   │    │
                        │  └─────────────┘    └──────────────────┘    │
                        │        │                    │               │
                        │        ▼                    ▼               │
                        │  ┌─────────────┐    ┌──────────────────┐    │
                        │  │  规则引擎     │───►│   告警/通知层      │    │
                        │  │ 告警规则      │    │ 通知路由/降噪/静默  │    │
                        │  └─────────────┘    └──────────────────┘    │
                        │        │                    │               │
                        │        ▼                    ▼               │
                        │  ┌──────────────────────────────────────┐  │
                        │  │           插件/命令注册中心            │  │
                        │  └──────────────────────────────────────┘  │
                        │        ▲                                    │
                        │  ┌─────────────┐   ┌──────────────────┐    │
                        │  │  对话层       │   │   图表/报表层      │    │
                        │  │ 意图识别→命令 │   │ matplotlib+模板   │    │
                        │  └─────────────┘   └──────────────────┘    │
                        └─────────────────────────────────────────────┘
                                 │ 通道①群Webhook  通道②长连接  通道③应用推送
                                 ▼
                    ┌──────────────────────────────┐
                    │         企业微信              │
                    │  群机器人 │ 智能机器人 │ 自建应用 │
                    └──────────────────────────────┘
```

### 技术选型

| 层 | 选型 | 理由 |
|----|------|------|
| 语言 | **Python 3.10+** | 官方 wecom-aibot-python-sdk、matplotlib 生态、AI 意图识别生态最全 |
| 服务框架 | FastAPI | 既有 Webhook/回调接收，也托管内部 API |
| 定时 | APScheduler | cron 表达式、misfire 处理、SQLite 持久化 job store |
| 存储 | SQLite（WAL 模式） | 单机零依赖；预留接口可换 PostgreSQL（对齐 newapi-monitor 反过度设计原则） |
| 采集 | httpx + tenacity | 异步、连接池、重试 |
| 图表 | matplotlib（中文字体注意）| 出 PNG → base64 → 群机器人 image 消息 |
| 意图 | 规则优先（命令表）+ LLM 兜底 | 规则零成本零延迟；LLM 理解自然语言（走 NewAPI 自家的 OpenAI 兼容口） |

---

## 4. 模块详细设计

### 4.1 采集层（Collector）

**两类采集：**

1. **巡检采集（定时拉取）**——APScheduler 任务：
   - `channel_snapshot`（每 5min）：`GET /api/channel/?page_size=100` → 存渠道快照（id/name/type/status/balance/priority/response_time）
   - `channel_probe`（每 10min，可配）：对启用渠道并发调 `GET /api/channel/test/:id`，记录 success/time/message → 存探测结果
   - `log_pull`（每 2min 增量）：`GET /api/log/?p=1&type=0&start_timestamp=...` 拉新增日志，解析出 error_code、耗时、渠道、模型、token 消耗 → 入库
   - `quota_daily`（每小时）：`GET /api/data/` 对账当日聚合

2. **轻量轮询（状态对比驱动）**——比全量测试更快发现异常：
   - 每 30s 拉一次渠道列表，对比 `status` 字段变化（1→3 = 自动禁用！这是最关键告警源）与 `balance` 变化，变化即产生事件
   - 错误日志率窗口统计（如 5min 内某渠道 error_code≠0 比例 > 阈值）

**NewAPI 客户端认证**：用管理员账号 `POST /api/user/login` 拿 session cookie，或更推荐——给监控专用一个**管理员用户**，并支持用其 access_token 方式。会话过期自动重登。

### 4.2 规则引擎与告警降噪

规则 = 条件表达式 + 级别 + 通知路由 + 降噪策略。**内置规则：**

| 规则 | 触发条件 | 级别 | 通知 |
|------|----------|------|------|
| 渠道自动禁用 | status 1→3 | 🔴 critical | 群机器人+自建应用私推管理员 |
| 渠道探测失败 | 连续 N 次(默认3) fail | 🟠 warning | 群机器人 |
| 渠道延迟劣化 | 探测 time > P95×3 且 >5s | 🟡 notice | 群机器人 |
| 余额不足 | balance < 阈值 | 🟠 warning | 群机器人 |
| 错误率飙升 | 5min 错误率 > 30% 且请求>10 | 🟠 warning | 群机器人 |
| 测试渠道成功→自动恢复 | status 3→1 或探测恢复 | 🟢 info | 群机器人（恢复通知） |

**降噪（借鉴 newapi-monitor 的低噪声设计）：**
- 指纹（rule+channel_id）→ 同一告警 30min 内不重复发（冷却期可配）
- 连续失败 N 次才触发（防抖），恢复通知自动发送
- 静默窗口（如 02:00-07:00 只发 critical）+ 值班表
- 每分钟最多 X 条（保护 webhook 20/min 限制）：超限合并为一条汇总

**告警状态机**：`firing → (确认/静默/恢复) → resolved`，事件存 SQLite，支持后续“今日告警回顾”。

### 4.3 对话层（智能机器人）

**接入**：`wecom-aibot-python-sdk` 长连接模式（无公网 IP 也能跑），事件 `message.text` 入口：

```python
@ws_client.on("message.text")
async def on_text(frame):
    content = frame["body"]["text"]["content"]  # 群聊: "@bot 渠道状态"
    chat_type = frame["body"]["chattype"]  # single / group
    userid = frame["body"]["from"]["userid"]
    # 去重(msgid) → 权限校验 → 意图识别 → 执行 → 流式回复
```

**指令路由（规则优先，LLM 兜底）：**

```
@机器人 渠道状态          → 所有渠道 status 摘要表（markdown 表格）
@机器人 渠道 12 详情       → 单渠道详情+最近探测+近24h错误率
@机器人 渠道 12 测试       → 实时调 /api/channel/test/12 并回结果
@机器人 日志 错误 今日     → 今日错误日志 top N
@机器人 用户 张三          → 用户额度/用量/分组
@机器人 图表 今日消耗       → PNG 图表回复（长连接 media 上传）
@机器人 图表 渠道延迟 7天   → 渠道延迟趋势折线图
@机器人 日报              → 每日总结（同 cron 推送内容）
@机器人 告警 列表/静默 渠道12 1h → 告警管理
@机器人 帮助               → 命令列表
自然语言兜底              → LLM(newapi 自家模型) 解析意图 → 调用同一命令层
```

**权限模型**：
- `admin`（企微 userid ↔ NewAPI 管理员映射）：全功能
- `viewer`：只读查询
- 未知用户：仅帮助/公开信息
- 私聊同权限，敏感操作（禁用/启用渠道）仅 admin 私聊可执行

**流式回复**：`reply_stream` 先回“查询中…”再流式输出长表格（窗口 10min 内完成）。

### 4.4 图表与报表层

- 模板：日消耗趋势（quota by day）、模型分布饼图、渠道延迟 P50/P95 折线、错误率堆叠图、用户消耗 Top10 条形图
- 中文字体：随包附带 `SourceHanSansSC` 或读取系统字体，避免方框
- 群机器人发图：`{"msgtype":"image","image":{"base64":...,"md5":...}}`（≤2MB）
- 长连接机器人发图：SDK 的 chunked media upload（init→chunks→finish）

### 4.5 每日总结（日报）

cron `0 9 * * *`（可配），汇总昨日 00:00–24:00：
- 调用量/消耗/RPM 峰值、活跃用户数、Token 消耗 Top 模型
- 渠道可用率排行（探测成功次数/总探测）、今日告警回顾（几条 critical/warning，恢复情况）
- 附 1–2 张图表
- markdown 推群 + 同内容存档 SQLite（供“@机器人 日报 昨天”回查）

---

## 5. 数据模型（SQLite）

```sql
channels      (id, name, type, status, priority, weight, balance, models_json,
               response_time, updated_at)                  -- 快照
probe_results (id, channel_id, success, latency_ms, message,
               error_code, probed_at)                      -- 探测流水
log_events    (id, newapi_log_id, created_at, type, user_id, channel_id,
               model_name, prompt_tokens, completion_tokens, quota,
               latency_ms, error_code, is_stream)          -- 增量拉取的日志
metrics_daily (date, channel_id, model_name, calls, errors,
               quota, p50_ms, p95_ms)                      -- 聚合表（cron 预聚合）
alerts        (id, fingerprint, rule_id, level, status, title, body,
               first_at, last_at, count, ack_by, resolved_at)
notify_queue  (id, alert_id, channel_type, target, payload, status,
               retries, next_retry_at, sent_at)            -- 投递队列+指数退避
bot_users     (wecom_userid, newapi_username, role, bound_at)
cmd_audit     (id, wecom_userid, command, args_json, ok, at)
```

聚合表 `metrics_daily` 由 cron 预聚合，图表/日报都读它（不扫流水）。

---

## 6. 扩展性设计（二次开发支持）⭐

这是你强调的重点，设计为**三层可插拔**：

### 6.1 插件体系

```python
# plugins/base.py
class PluginMeta(type):
    registry = {}


class MonitorPlugin(metaclass=PluginMeta):
    name: str
    version: str

    # —— 采集扩展：注册定时任务 ——
    def collectors(self) -> list[CollectorSpec]: ...  # 返回采集规格
    # —— 告警扩展：注册规则 ——
    def rules(self) -> list[RuleSpec]: ...  # 返回告警规则
    # —— 对话扩展：注册命令 ——
    def commands(self) -> list[CommandSpec]: ...  # 返回机器人命令
    # —— 通知扩展：注册通道 ——
    def notifiers(self) -> list[NotifierSpec]: ...  # 飞书/TG/邮件/短信…
    # —— 面板扩展（后期）——
    def pages(self) -> list[PageSpec]: ...  # Web 控制台页面
```

**热加载**：扫描 `plugins/` 目录 + 入口点（`pyproject.toml [project.entry-points."newapi_monitor.plugins"]`），pip 安装的第三方插件也能被发现。每个插件独立开关（配置项 `plugins.enabled = ["balance_forecast", ...]`）。

### 6.2 预留的扩展方向（后期功能，架构已留口）

| 方向 | 怎么接 |
|------|--------|
| 更多 IM（飞书/TG/钉钉） | 实现 `Notifier` 接口即可接入通知路由 |
| Web 控制台 | FastAPI 已在，加 Jinja2/React 页面，读同一 SQLite |
| 多 NewAPI 实例监控 | 采集层支持多 endpoint 配置，数据加 `instance_id` 维度 |
| MCP Server 化 | 命令层天然就是 tool 集合，包一层 MCP 协议（目录名就叫 newapi-mcpserver，可顺路做）|
| 自动自愈（禁用→重试→自动启用） | 写一个 AutoHeal 插件：监听告警恢复规则，调 `/api/channel/:id/status` |
| 余额预测/成本预算 | 读 metrics_daily 做回归，Notifier 推送 |
| 语音查询 | 智能机器人私聊 voice 已自动转文本，直接进意图层 |

### 6.3 配置驱动

```yaml
# config.yaml
newapi:
  base_url: https://your-newapi.example.com
  admin_user: monitor_bot
  admin_password: "***"          # 或 session/token
collectors:
  channel_snapshot: {enabled: true, cron: "*/5 * * * *"}
  channel_probe:   {enabled: true, cron: "*/10 * * * *", models: ["gpt-4o-mini"]}
  log_pull:        {enabled: true, interval: 120}
wecom:
  webhook_group: {url: "https://qyapi.weixin.qq.com/..."}   # 通道①
  aibot: {bot_id: "...", secret: "..."}                     # 通道② 长连接
  app_push: {corpid, corpsecret, agentid, toadmins}         # 通道③
llm_router:                    # 自然语言兜底（可关）
  base_url: https://your-newapi.example.com/v1
  model: gpt-4o-mini
plugins:
  enabled: [daily_report, auto_heal]
alerting:
  cooldown_min: 30
  consecutive_failures: 3
  quiet_hours: ["02:00-07:00"]
```

---

## 7. 目录结构（建议）

```
newapi-mcpserver/
├── docs/                      # 本方案 + API 调研笔记
├── src/newapi_monitor/
│   ├── main.py                # 入口：启动 FastAPI + scheduler + aibot ws
│   ├── config.py              # yaml 配置加载
│   ├── collectors/            # 采集器（每个文件一个）
│   │   ├── channels.py  logs.py  probe.py  quota.py
│   ├── rules/                 # 规则引擎 + 内置规则
│   ├── notify/                # 三通道 + 队列投递
│   │   ├── wecom_webhook.py  aibot_ws.py  app_push.py  queue.py
│   ├── bot/                   # 对话层
│   │   ├── handlers.py       # 事件入口/去重/权限
│   │   ├── intents.py        # 规则+LLM 意图识别
│   │   └── commands/         # 每个命令一个文件（命令即插件）
│   ├── charts/                # matplotlib 模板
│   ├── store/                 # SQLite models + 聚合
│   ├── newapi_client/         # NewAPI 管理 API SDK（登录/渠道/日志/数据）
│   └── plugins/               # 插件加载器 + 内置插件
│       ├── daily_report.py  auto_heal.py  ...
├── plugins/                   # 外部插件目录（用户放这里）
├── tests/
├── config.example.yaml
├── pyproject.toml             # 含 entry-points 插件发现
└── Dockerfile + docker-compose.yml
```

---

## 8. 实施路线（建议 4 期）

**M1（第 1 周，核心闭环）**
- NewAPI 客户端（登录/渠道列表/单渠道测试）
- channel_snapshot + 状态对比 → 检测 1→3 自动禁用
- 群机器人 webhook 推送（告警模板+冷却）
- ✅ 验收：手动禁用一个渠道，群里 1 分钟内收到告警

**M2（第 2 周，对话能力）**
- 智能机器人长连接接入（官方 SDK）
- 命令：渠道状态/详情/测试/帮助 + 权限映射
- 增量日志采集 + 错误率窗口规则
- ✅ 验收：群 @机器人 和私聊都能查渠道

**M3（第 3 周，图表与日报）**
- metrics_daily 预聚合、matplotlib 模板、图片推送（两条通道）
- 每日总结 cron + 日报回查命令
- ✅ 验收：每天 9 点自动收到带图日报

**M4（第 4 周，扩展体系）**
- 插件加载器 + entry-points + 示例插件（auto_heal 自动恢复演示）
- Docker 化 + docker-compose（含健康检查）
- LLM 意图兜底接 NewAPI 自家 /v1
- ✅ 验收：第三方 pip 插件能被发现并注册命令

---

## 9. 风险与对策

| 风险 | 对策 |
|------|------|
| webhook 20条/分钟限制 | 告警合并不超过 18/min，溢出转汇总条 + 自建应用补发 |
| 智能机器人长连接单连接 | 进程单实例部署；断线自动重连（SDK 支持 + 自守护） |
| NewAPI 登录会话过期 | 401 自动重登 + 探测任务失败重试（tenacity） |
| 渠道测试本身消耗 token | 探测用便宜模型（gpt-4o-mini 类）+ max_tokens=1 + 可配频率 |
| 企微 userid 加密（非超管创建者） | 用“自建应用↔机器人”集成转换，或直接以超管身份创建 |
| markdown 表格在旧客户端显示 | 兼容性降级：文本对齐表格 |
| SQLite 写入竞争 | WAL 模式 + 单写连接（write queue） |
| 中文字体缺失（Docker） | 镜像内附带字体文件 |

---

## 11. 方案路线全景对比（v2 · 不绑定 MCP）

> 开放思路：同一需求有 5 条可行路线，MCP 只是其中之一。核心差异在**数据怎么来**（轮询 / 事件推送 / 探测）和**对话层怎么建**（自研 / 二开框架）。

### 路线总览

| 路线 | 数据获取 | 对话层 | 公网要求 | 开发量 | 适合场景 |
|------|----------|--------|----------|--------|----------|
| **A. 纯轮询自研** | 定时调管理 API + 主动探测 | wecom-aibot-python-sdk 自研命令路由 | ❌ 不需要（长连接） | 中 | 最小可控，第 3–4 章方案即此路线 |
| **B. 事件驱动（推荐增强）** | NewAPI 自带 webhook 事件（禁用/恢复/测试完成）+ 低频轮询兜底 | 同 A | ✅ 需公网回调（仅事件口） | 中 | 告警实时性最好（秒级 vs 分钟级轮询） |
| **C. 二开 chatgpt-on-wechat（CowAgent）** | 写一个插件调 NewAPI API | **复用现成**：多渠道/插件/MCP/技能市场 | 企微自建应用渠道需公网；智能机器人长连接部分不需要 | **小** | 想要“聊天机器人产品感”、后续接其他 IM |
| **D. MCP Server 路线** | MCP tools 封装管理 API | AI 客户端（Claude/Cursor/Cow）当对话层 | 取决于宿主 | 中 | 想让任何 AI 助手都能管理 NewAPI |
| **E. 二开监控项目**（newapi-monitor / relay-status-monitor） | 复用其采集/告警实现 | **缺对话层，需补** | 视项目 | 中大 | 只想要监控面板+告警，不要对话 |

### 各路线详评

**路线 A（基线）**：第 3–8 章完整方案就是它。30s 渠道列表轮询发现 status 变化 + 10min 探测 + 2min 日志增量。优点：零公网（智能机器人走长连接 wss 出站）、所有逻辑自己掌控。缺点：最快 30s 延迟；轮询 QPS 是常驻开销。

**路线 B（A 的事件增强，推荐）**：NewAPI 源码已证实渠道自动禁用/恢复时会发 webhook 通知（`service/channel.go` → `NotifyChannelWatchers` → `SendWebhookNotify`，payload: `Type/Title/Content/Timestamp`，HMAC-SHA256 签名头 `X-Webhook-Signature`）。做法：
1. 管理员在 NewAPI 后台把 webhook 指向本服务公网地址（可用 cloudflare tunnel/frp 免买公网 IP）
2. 本服务收事件 → 验签 → 入库 → 立即推企微群
3. 保留低频轮询（5min）做兜底对账，防事件丢失
   - 优点：**秒级告警**、NewAPI 零改造、事件即权威
   - 缺点：需要一个能被 NewAPI 访问的回调口（frp/tunnel 可解）
   - ⚠️ 注意：webhook payload 不含 channel_id 字段（只有 Title/Content 文本），需要从 Content 解析渠道名，或收事件后立刻调 `GET /api/channel/` 反查

**路线 C（二开 CowAgent）**：[chatgpt-on-wechat](https://github.com/zhayujie/chatgpt-on-wechat)（已更名 CowAgent，MIT，~70% Python）是成熟的对话机器人框架：
- 已支持渠道：微信、企微（自建应用 + Bot 两种）、飞书、钉钉、Telegram、QQ、Discord、Slack…，**一个实例多渠道并行**
- 已有：Web 控制台、插件/Skill 体系（Skill Hub 一键装）、MCP 原生集成、多 LLM 接入（可直连你的 NewAPI /v1）
- 企微自建应用渠道：需公网 IP + 回调（`/wxcomapp`，9898 端口，Token/EncodingAESKey）+ 企业可信 IP
- **二开方式**：写一个 `newapi-monitor` 插件（Skill），注册“渠道状态/日报/图表”等命令；监控 cron 在插件内跑，告警经框架的通知能力推群
- 优点：对话层/权限/多 IM 白送，社区活跃；缺点：框架较重（为通用聊天设计），监控数据层仍要自建；企微渠道要求公网+备案域名（认证企业）

**路线 D（MCP，可选不必须）**：把管理 API 封装成 MCP tools（已有参考 Ruanyuxi1337/newapi-operator，Go 版）。对话层交给任意 MCP 客户端——包括 CowAgent（原生 MCP 支持）。适合作为路线 C 的补充：“群机器人（CowAgent）→ 调 MCP server（本项目）→ 操作 NewAPI”。当前对话（ZCode 等 IDE）也可直接用它管理 NewAPI。

**路线 E（二开监控项目）**：
- li0on3/newapi-monitor（Python FastAPI+React，MIT）：监控+告警最全（含企微群+应用推送、告警队列、SSO），**但无对话机器人、无企微智能机器人集成** → 二开=补一个 aibot 长连接模块
- yigehaozi/relay-status-monitor（TS Next.js，MIT）：采集分层好，但通知是飞书 → 二开=把飞书通知层换成企微 + 补对话层，跨语言成本高
- baogutang/api-monitor（Go）：轻量监控告警，同样缺对话层

### 推荐组合（按需求开放度排序）

1. **首选：B + A 混合自研**（事件驱动告警 + 轮询兜底 + 长连接对话）——最贴合你的原始需求，零公网可跑（若要事件则 frp/tunnel）
2. **次选：C 路线二开 CowAgent**——如果你更看重“机器人产品化”和未来多 IM 扩展，开发量最小
3. **后期加 D**：把命令层包成 MCP server，让 IDE/CowAgent 等任何客户端也能查 NewAPI

---

## 12. 二开项目改造成本评估

| 项目 | 语言/协议 | 已有能力 | 缺什么 | 二开工作量 | License |
|------|-----------|----------|--------|-----------|---------|
| chatgpt-on-wechat (CowAgent) | Python | 多 IM 渠道/插件/Skill/MCP/多 LLM/控制台 | NewAPI 采集+告警+图表逻辑（全要写成插件） | **小–中**（1–2 周出 M1） | MIT |
| li0on3/newapi-monitor | Python | 渠道探测/延迟/日志/资源监控、企微+飞书告警、队列重试、SSO、控制台 | 企微智能机器人对话层、图表命令 | **中**（2 周出对话闭环） | MIT |
| Ruanyuxi1337/newapi-operator | Go | NewAPI 管理 API 的 MCP 封装 | 监控/告警/图表/企微全缺 | 中大（等于重写） | 待查 |
| yigehaozi/relay-status-monitor | TypeScript | 余额/延迟/测速/飞书告警 | 企微、对话层 | 中大（跨语言） | MIT |

**二开建议**：
- 若主攻“对话机器人体验” → 二开 **CowAgent**（写插件），监控 cron 放插件内
- 若主攻“监控告警可靠性” → 二开 **li0on3/newapi-monitor**（加 aibot 长连接模块），其告警队列/降噪已成熟
- 若走自研 → 按第 3–8 章方案实施，第 13 章为 B 路线（事件驱动）补充设计

---

## 13. 路线 B 补充设计：NewAPI 事件接入（v2 新增）

```
NewAPI (渠道禁用/恢复/测试完成事件)
   │ POST webhook (HMAC-SHA256 签名, payload: Type/Title/Content/Timestamp)
   ▼
本服务事件口 (FastAPI POST /webhook/newapi)
   │ ① 验签(X-Webhook-Signature) ② msgid/时间戳防重放 ③ 从 Content 解析渠道名
   │ ④ 调 GET /api/channel/ 反查最新状态（权威数据）⑤ 入库并生成告警
   ▼
规则引擎（冷却/静默/合并）→ 企微群机器人推送（秒级）
```

要点：
- **公网接入方案**（无公网 IP 时）：cloudflare tunnel（免费、免备案）> frp（有 VPS 时）> nginx 反代
- 事件与轮询**双写幂等**：以 `(事件类型, 渠道, 时间窗)` 为幂等键，事件先到则轮询跳过，轮询先到则事件只更新
- Content 是 i18n 文本（含渠道名和原因），解析正则按 NewAPI 版本锁定；解析失败时降级为“收到未知事件，已轮询对账”
- NewAPI 管理后台配置入口：管理员设置 → 通知方式 webhook（URL + secret），root 默认收，其他管理员 opt-in

---

## 14. 参考

- NewAPI: https://github.com/QuantumNous/new-api · 文档 https://docs.newapi.pro/
- 渠道路由源码: `router/channel-router.go`；渠道测试返回: `controller/channel-test.go`
- **NewAPI 事件通知源码（v2 核实）**: `service/channel.go`（DisableChannel/EnableChannel → NotifyChannelWatchers）、`service/user_notify.go`（邮件/webhook/Bark/Gotify 四通道）、`service/webhook.go`（HMAC-SHA256 签名 webhook）
- 企微群机器人 webhook: developer.work.weixin.qq.com/document/path/91770
- 企微智能机器人: /document/path/101039（概述）· 101463（长连接）· 100719（接收消息）· 101138（response_url 主动回复）
- 企微自建应用推送: /document/path/90236
- 官方 Python SDK: https://github.com/WecomTeam/wecom-aibot-python-sdk (`pip install wecom-aibot-python-sdk`)
- 同类参考项目: li0on3/newapi-monitor · yigehaozi/relay-status-monitor · Ruanyuxi1337/newapi-operator
- **二开框架**: https://github.com/zhayujie/chatgpt-on-wechat（CowAgent，MIT，多 IM 插件化）· 文档 https://docs.cowagent.ai/channels/wecom
