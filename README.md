# NewAPI Watch

当前源码开发版本：**监控策略优先（pure-admin 管理台）**；Python 包元数据版本为 0.4.0。多平台管理台、平台专属机器人与 Webhook、真实数据工具问答、OpenAI Chat / DeepSeek 兼容、亮白/暗黑主题。设计和验证说明见 `docs/管理台重设计-Chat兼容.md`。

> 开发默认管理台入口：`http://127.0.0.1:8848/`，唯一管理账号 `admin`。密码只保存在本机 `data/console/initial-account.txt`，不要发到群聊。导入后的平台以 Web 保存的配置为准，后续修改旧 `.env` 不会覆盖 Web 配置。

> **当前只做本地开发：** Vue 热更新入口 `http://127.0.0.1:8848/`；源码 API 后端 `http://127.0.0.1:8090/`（该端口静态资源可能是旧构建，日常开发请使用8848）。后端使用 `.venv-dev` 运行源码，本轮未安装新 wheel、未构建或发布 Docker。`.venv-console` 保留为上一版环境，不是当前开发入口；不要同时启动相同的机器人绑定。

> 模型服务 → 填写接口地址、服务方模型 ID 与调用 Key → 兼容模式默认“自动识别” → 保存 → 测试已保存的连接。DeepSeek 官方地址可填 `https://api.deepseek.com`；也支持基础 `/v1` 地址或完整 `/chat/completions` 端点。

独立部署的 NewAPI 监控告警与企业微信运维机器人。**不修改 NewAPI，不需要为升级监控服务而重启中转站。所有机器人使用者权限一致，不做账号、角色、身份绑定或多用户平台。**


## 首次从 GitHub 获取并运行

需要 Python 3.12+、Node.js（版本要求见 `frontend/.nvmrc`）、uv 和 pnpm 10.33.0。
仓库不包含真实 `.env`、数据库、密钥或预置密码，首次运行会生成你自己的本地管理账号文件。

```powershell
git clone https://github.com/shangqiu0/newapi-watch-weixin.git
cd newapi-watch-weixin
uv sync --frozen --extra dev
pnpm --dir frontend install --frozen-lockfile

# 仅首次初始化；初始密码只写入 data/console/initial-account.txt
uv run --frozen --extra dev newapi-watch console-init --control-dir data/console --username admin

# 终端一：启动本地 API（insecure-cookie 仅限本机 HTTP 开发）
uv run --frozen --extra dev newapi-watch console --control-dir data/console --host 127.0.0.1 --port 8090 --insecure-cookie

# 终端二：启动前端热更新页面
pnpm --dir frontend dev
```

打开 `http://127.0.0.1:8848/` 登录，在「平台管理」新增自己的 NewAPI 平台，再配置策略及机器人。
`.venv-dev`、`.venv-console` 等目录只属于原开发机器，不是克隆仓库后必须存在的环境；首次克隆使用上面的 uv 命令即可。
请保护整个 `data/console/`，尤其是 `master.key`；勿将账号文件、令牌、数据库或私人对话导出提交到仓库。
前端沿用 pure-admin 的源码和 MIT 许可，见 `frontend/LICENSE`。

## 本地前端开发（新入口）

前端已按用户 pure-admin 方案完整迁移到 `frontend/`，不再保留原生 `src/newapi_watch/web/`。15 个业务页面使用 Vue 3、Pinia、Element Plus/PureTable 与 ECharts。迁移、测试和真实接口边界见 `docs/v0.5-pureadmin迁移与验收.md`。

```powershell
# 后端：当前已运行时不要重复启动；仅本机 HTTP 开发使用 insecure-cookie
.\.venv-dev\Scripts\python.exe -m newapi_watch console --control-dir data/console --host 127.0.0.1 --port 8090 --insecure-cookie

# 另一个终端：前端开发，依赖已安装时直接 dev
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend dev

# 验证，不是发行部署
pnpm --dir frontend typecheck
pnpm --dir frontend test
pnpm --dir frontend audit --prod
```

前端首次静态验收可执行 `pnpm --dir frontend build` 生成本地 `web-dist`；日常开发只用热更新，不需要反复打包。未来部署再单独执行和验证发行/容器流程。修改 Python 依赖前先停止本项目开发服务。

## 当前核心：策略检查，而不是业务数据仓库

```
配置策略 → 定时访问绑定平台 API → 完整性校验 → 条件/事件判断 → 去重与冷却 → 本平台企业微信通知
```

- **监控工作台**：检查启停、最近成功/下一次检查、未知/逾期、当前异常与通知队列，不以已采集数据量为主指标。
- **监控策略**：渠道自动禁用/缺失、请求错误状态码与模型/渠道/错误码过滤、连续确认、冷却、来源失败提醒和同平台Webhook目标。
- **余额监控**：指定上游用户，独立阈值、检查间隔、恢复阈值、启停与通知目标。缺少quota字段或接口失败是未知，不是0。
- **报告调度**：日报周报独立启停，只有到时才查询上游并内存汇总。
- **通知投递**：正式通知的待发/发送/成功/失败记录、幂等重试、逐目标成功收据。
- 默认首次错误检查从当前时刻建基线，不追发历史；需要时可设置首次回看。
- 新Console监控任务不再持续写入业务logs表；仅保留当前快照、条件状态、游标、脱敏证据与通知。按需日志/统计查询仅短期内存缓存，不推进监控游标。
- 不修改NewAPI；监控确定性执行，不依赖LLM、网页打开或聊天请求。单管理员、无注册、多平台/机器人隔离不变。

设计、默认参数、接口与本地验收边界见 `docs/监控策略优先-实现与验收.md`。

## 平台管理

在侧栏「平台管理」或顶栏同名入口管理所有接入站点：新增、编辑、启用/停用、删除。
编辑可修改名称、管理令牌（留空保留）、管理用户ID和时区；地址创建后仍只读，换站点请新增平台。
删除须输入平台名称确认，会停止此平台监控与机器人连接并移出列表，本地历史和加密配置保留，不修改上游NewAPI或企业微信机器人；当前不提供网页恢复/永久清理。
删除当前平台后自动选择剩余平台，删除最后一条后仍能在管理页重新新增。
实现、迁移备份与验证边界见 `docs/2026-11-13-平台管理实现.md`。

## 已实现

- 无注册的单管理账号 Web 控制台；每个平台独立 SQLite 数据、加密凭证与固定机器人绑定，不做多用户平台。
- 系统/本地持久化双主题、等宽 KPI、真实用量折线/面积图、渠道筛选、告警、绑定、模型诊断、问答与审计页面。
- 统一 OpenAI Chat Completions 模型协议；DeepSeek 官方端点自动使用非思考工具工作流，其他接口不接收 DeepSeek 专有参数。

- 管理 API 完整分页、白名单 DTO、连接超时与只读请求重试；分页总数变化/不完整会拒绝发布结果。最小监控状态使用 SQLite WAL 持久化。
- Console模式使用可配置的渠道状态、错误事件、逐用户余额和来源失败策略；旧CLI/serve保留兼容性采集与主动探测，不是当前网站推荐启动入口。
- 事务告警状态机和通知 outbox；重启持久化、同一故障去重、退避重试、死信、全局群通知限速。
- 企业微信群 Webhook 告警和带图日报；企微智能机器人官方 SDK 长连接，群 @ / 私聊共用命令层。
- 查询渠道、用户额度/用量、错误记录；多日统计、渠道/模型/用户排行、PNG 趋势/排行图、每日总结、告警确认与静默。
- 死信列表与幂等重试；脱敏命令审计与组件新鲜度/机器人/后台任务健康信息。
- 可选 NewAPI HMAC Webhook 接收、防重放、旧版字段大小写兼容；事件只触发状态核查，不直接覆盖状态。
- HTTP 运维 API、离线 DEMO、CLI、可信安装包扩展入口、Docker/Compose 和 CI 配置。

模型问答是可选能力，监控与告警不依赖模型。未引入 MCP、自动启停/删除渠道、语音处理或企微自建应用定向推送；未做多用户注册、用户角色分级或通用聊天平台。

## 1. 安装与离线体验

需要 Python 3.12+，推荐 uv。Windows PowerShell：

```powershell
cd D:\work\newapi-mcpserver
uv sync --frozen --extra dev
uv run newapi-watch --demo command "渠道状态"
uv run newapi-watch --demo command "图表 今天" --image artifacts/demo-report.png
uv run newapi-watch --demo serve --port 8088
```

不使用 uv：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements.txt
.venv\Scripts\python -m pip install --no-deps -e .
.venv\Scripts\python -m newapi_watch --demo command "统计"
```

DEMO 使用 `data/demo.sqlite3`，不访问真实 NewAPI、不连接真实企微、不发送真实通知。模拟投递会标记 sent 并写 `demo_notification` 日志，**这不代表企微已经收到消息**。

## 2. 生产配置

1. 复制 `.env.example` 为 `.env`。
2. 填写 `WATCH_NEWAPI_URL`、`WATCH_NEWAPI_TOKEN`、`WATCH_NEWAPI_USER_ID`。
3. 设置随机 `WATCH_API_KEY`（至少24位）。可用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成。
4. 填写群通知 Webhook；如果不填，生产模式不会伪装投递成功，通知保持 pending。
5. 需要群聊/私聊查询时，填写企微智能机器人的 `WATCH_BOT_ID` / `WATCH_BOT_SECRET`，使用长连接模式。

```powershell
uv run newapi-watch check
uv run newapi-watch once
uv run newapi-watch serve --host 127.0.0.1 --port 8088
```

**注意：** NewAPI 管理访问令牌不是模型转发 `sk-` 令牌。当前适配器发送 `Authorization: Bearer ...` 与 `New-Api-User`；你的实例版本、令牌权限和反向代理规则需要实际联调确认。没有实现保存管理员密码或自动登录。

机器人参与者都能查询平台用户信息，因此只把机器人放到你信任的内部群/会话。统一权限不等于公开服务：HTTP 使用统一服务密钥，NewAPI 事件验 HMAC，程序不输出 API key、渠道 key、用户密码或完整日志内容。

## 3. 使用命令

| 输入 | 作用 |
|---|---|
| `帮助` | 命令说明 |
| `渠道状态` | 渠道快照和状态（最多50条；HTTP可分页查询全部） |
| `渠道 12 详情` | 渠道详情和24小时成功/错误记录 |
| `渠道 12 测试` | 显式主动测试；需统一开启 `WATCH_PROBE_ENABLED` |
| `用户 张三` | 查询用户额度、用量、分组，最多20条 |
| `日志 错误 今日` | 今日已采集错误记录，不返回原始错误正文 |
| `统计` / `统计 近7天` | 今日或最多31天的统计文字 |
| `排行 模型 近7天` / `排行 用户 昨天 按调用` | 按模型/用户/渠道聚合排行 |
| `图表 渠道 近7天` | 渠道消耗排行PNG，文本中提供ID对应项 |
| `图表 2026-11-01~2026-11-03` | 指定自然日范围的每日趋势PNG |
| `图表 今天` / `图表 2026-11-09` | 带图统计 |
| `日报` / `日报 昨天` / `日报 2026-11-09` | 每日总结，默认昨天 |
| `告警 列表` | 最近告警及恢复状态 |
| `告警 静默 渠道12 1h` | 静默后续新通知；仍记录状态，已入队消息不撤回 |
| `告警 取消静默 渠道12` | 取消静默 |
| `告警 确认 <告警ID>` | 记录知晓，不代表恢复；再次触发需要重新确认 |
| `告警 静默列表` | 查看当前有效静默 |
| `通知 死信` / `通知 重试 <通知ID>` | 查看失败通知或幂等重新入队 |
| `审计 列表` | 查看命令来源/类别/结果/耗时，不展示原始输入 |
| `健康` | 查看数据新鲜度、队列、任务与机器人状态 |

群内使用 `@机器人 渠道状态`，私聊直接发送命令。没有 sender 的角色分支。当前是明确命令及少量别名，不声称支持任意自然语言理解。

主动探测会访问 NewAPI 的测试接口，可能消耗额度、更新上游测试状态；默认关闭，不会自动重试测试。群里的所有人都可以使用已经统一开启的功能。

## 4. HTTP 运维接口

- `GET /health/live`：进程存活，公开，不表示采集/通知正常。
- `GET /health/ready`：渠道数据是否足够新鲜，未就绪返回503。
- 以下接口需要 `X-API-Key`：
  - `GET /api/v1/health`
  - `GET /api/v1/channels?page=1&page_size=20`（最多100/页）
  - `GET /api/v1/alerts?limit=20`
  - `GET /api/v1/notifications?limit=20`
  - `POST /api/v1/commands`，JSON `{"text":"渠道状态"}`
  - `GET /api/v1/reports/2026-11-09/chart`，直接返回 `image/png`
  - `GET /api/v1/schema`，受保护 OpenAPI JSON
- `POST /webhooks/newapi`：单独使用 HMAC，不用 HTTP 服务密钥。

测试/设置或取消静默/确认/重试命令必须带 `Idempotency-Key`（8–128位）；重复键返回409，不重复执行。普通只读命令不要求幂等键。HTTP和企微都有查询并发限制，超时不会拖住监控主循环。

PowerShell 示例（凭证从你的环境变量读取，不要把真实密钥发到群里）：

```powershell
$headers = @{ 'X-API-Key' = $env:WATCH_API_KEY }
Invoke-RestMethod http://127.0.0.1:8088/api/v1/channels -Headers $headers
$body = @{ text = '渠道状态' } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8088/api/v1/commands -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

程序读取 `.env` 不会把变量注入你的 PowerShell；上面的 `$env:WATCH_API_KEY` 需要你自己设置。

## 5. 事件、统计与通知语义

### 事件接入

设置 `WATCH_WEBHOOK_ENABLED=true` 和至少16位随机 `WATCH_WEBHOOK_SECRET`，在 NewAPI 配置同一 secret 与本服务 `/webhooks/newapi` 地址。验证原始 body 的 HMAC-SHA256 十六进制 `X-Webhook-Signature` 和 payload 内 `timestamp`，**不依赖上游不存在的时间戳 header**。

地址只需要对 NewAPI 可达，不必无条件暴露公网；但上游 SSRF 策略可能限制私网地址。没有事件能力时轮询照常运行。收到任何受信事件只持久化核查标记，不根据通知文本或旧状态直接告警；短暂变化可能被合并，持续状态由对账保证。

### 数据口径

- `type=2` 统计消费记录，`type=5` 统计错误记录。重试可能产生多个日志，**错误记录占比不是最终用户请求失败率**。
- `quota` 为整数原始单位，不擅自换算人民币/美元。
- 图表按UTC小时桶聚合、按配置时区显示；日报自然日边界使用 ZoneInfo，支持夏令时23/25小时日期。
- 新启动默认回拉24小时；超过保留期、首次采集缺历史、采集落后会标注“不完整”。空采样不会被当作故障已恢复。
- 所有日志ID去重。重叠窗口默认120秒，只能覆盖该窗口内的晚到日志；更晚的数据需调大窗口或手工回补。分页不支持上游快照隔离时，频繁变化的数据仍应通过对账检测。
- 日报默认本地9点发送昨天，重启只补最近一天，不补发全部历史日报。图表与正文分别入队，避免同一消息超限。

### 投递保证

- 告警变化和入队同事务，发送在事务之外；同一故障持续期间不重复创建通知，恢复单独入队。
- 单群全局默认3.4秒/条；重启后保留限速时间。异常时指数退避，最多8次，之后标记dead。
- 按队列顺序发送，前一条退避期间后一条等待，避免恢复消息先于告警。网络发送成功但落库前崩溃，可能重复发送：**至少一次，不保证恰好一次**。
- dead可通过 `通知 死信` 或受保护接口查看；核查原因后使用 `通知 重试 <ID>` 单条重新入队，持久化幂等、保留发送限速，不自动无限重发。历史文字通知会加重试标记，重发图片仍为原始历史图片。
- 完整实时性依赖接口可用、企微限额和网络；不承诺任何情况下“秒级必达”。

## 6. Docker / Redis Compose

已提供独立 Redis 容器：`compose.yaml` 启动 `watch` 与 `redis`，`compose.console.yaml` 切换为当前多平台网页管理台。**这里只准备部署配置；本轮没有构建或发布新的应用镜像。**

### 推荐：多平台网页管理台（服务器部署时执行）

```bash
# 仅首次复制，不覆盖已有.env
[ -f .env ] || cp .env.example .env

# 构建当前源码镜像，生产依赖已包括Redis Python客户端
# 后续在服务器准备好部署时再执行，不是日常开发命令
docker compose -f compose.yaml -f compose.console.yaml build watch

# 仅首次初始化管理员；数据库和密钥写入watch-data卷，不会启动机器人
# 已有data/console管理数据时不要重复初始化
docker compose -f compose.yaml -f compose.console.yaml run --rm --no-deps watch console-init --control-dir /app/data/console --username admin

# 初始密码只写入卷内文件；在自己的服务器终端读取并妥善保管
# 不要将输出发到群聊或提交到仓库
docker compose -f compose.yaml -f compose.console.yaml run --rm --no-deps --entrypoint cat watch /app/data/console/initial-account.txt

# 一起启动网页管理台与Redis
docker compose -f compose.yaml -f compose.console.yaml up -d
```

网页仅监听宿主 `127.0.0.1:8088`。生产通过 HTTPS 反向代理访问；管理台默认安全Cookie，**不要为了公网HTTP关闭Cookie安全保护**。首次登录后在网页新增平台、模型和机器人，网页配置保存在加密控制数据库中，不需要把各平台凭证全部写进 `.env`。

### Redis默认行为

- 官方 Redis 镜像使用已验证版本与内容摘要固定，不使用浮动 `latest`；升级时需重新验证并更新镜像引用。
- 自动连接 `redis://redis:6379/0`，无需手动安装Redis或开放6379端口。
- Redis仅接入专用 `internal` 缓存网络；只有应用同时接入缓存网络和可出站网络。
- 启动时等待Redis健康；运行中Redis不可用时应用仍可回退内存缓存，任务进度由SQLite保存。
- 缓存上限128MB、容器内存上限256MB，采用LRU淘汰；关闭RDB/AOF，缓存可丢弃，不新增Redis持久化卷。
- `watch-data` 卷仍保留监控数据库、任务进度、控制台密钥和账号，不能与缓存一起清空。
- 如需外部Redis，可在 `.env` 设置 `WATCH_QUERY_REDIS_URL`；显式设置为空字符串可关闭应用Redis缓存。Compose仍会创建内置Redis容器；外部Redis的安全与认证需自行配置。

### 保留旧的单平台模式

`docker compose up -d --build` 不加覆盖文件时，仍沿用镜像的旧 `serve` 启动命令；必须先填写该模式需要的 `.env` 凭证。**当前多平台查询任务功能请使用上面的Console组合。**

**必须单实例、单worker。** Redis只是缓存，不是多实例调度器；不要让多副本争抢同一个企微机器人连接或共用同一个SQLite数据库。

后续升级只重建/重启监控服务，不操作NewAPI。当前平台业务数据库schema为v6，控制台数据库schema为v2；升级前先备份，回滚旧程序需恢复升级前的数据。`docker compose down`默认保留数据卷；不要随意添加 `-v`。更完整的Linux部署、日志排查和备份说明见 [Docker Compose部署说明](docs/部署-Docker-Compose.md)。

## 7. 二次开发

源码位于 `src/newapi_watch`；依赖方向：适配器 → DTO → 采集/规则 → Store/outbox，HTTP与企微共用 `Commands`。

支持可信已安装Python包的入口点：

```toml
[project.entry-points."newapi_watch.plugins"]
my_extension = "my_extension:register"
```

```python
from newapi_watch.models import CommandResult


def register(registry, runtime):
    async def status(text):
        return CommandResult(text="扩展工作正常")

    async def collect():
        # 采集外部数据后可通过 runtime.store.condition(...) 产生告警
        # 不要在事务中进行网络调用
        return None

    registry.command("扩展状态", status)
    registry.collector("my_extension", 60, collect)
```

在环境中设置 `WATCH_PLUGINS=["my_extension"]` 后重启监控服务。不要从群聊加载任意文件或安装不可信插件。命令、采集任务可注册；规则通过condition接口复用状态机，通知适配器实现Notifier协议并由部署代码注入。**没有实现任意插件代码热加载或可视化插件市场。**

后续加 MCP 时封装现有命令/查询层即可，不重写业务逻辑。此版不复制参考项目代码，保留参考来源在 `docs/实现说明与验证.md`。

## 8. 验证

```powershell
uv run --frozen --extra dev pytest --cov=newapi_watch --cov-report=term-missing
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv run --frozen --extra dev mypy src/newapi_watch
uv run --frozen --extra dev pip-audit --skip-editable
pnpm --dir frontend test
```

测试使用真实SQLite、真实httpx适配器、真实企微SDK回复构造；只替换外部传输。不使用真实平台密钥，也不声称等同于真实企业微信端到端验收。当前验证结果、局限和人工联调清单见 `docs/实现说明与验证.md`。
v0.2已完成Windows和Linux容器两套全量验证：各125项通过，覆盖率90.37%；Ruff、mypy、Python依赖审计、wheel/sdist打包及Docker运行验收均通过。仍有2条第三方SDK弃用警告；已完成真实NewAPI只读接口、企微群Webhook服务端接收与智能机器人长连接认证；群@/私聊触发交互及图片显示仍需端到端验收。

v0.4.0 本轮 Windows / Linux 全量各 172 项通过，覆盖率 88.58%；前端主题与图表 9 项通过。真实 DeepSeek Chat、工具能力与合成工具往返验证通过；真实企微群聊/私聊端到端消息仍需人工发起确认。详细证据见 `docs/管理台重设计-Chat兼容.md`。

需要重跑容器验收时（PowerShell 7）：

```powershell
docker build -t newapi-watch:local .
pwsh -File tests/verify_container.ps1
docker build -f tests/Dockerfile.validation -t newapi-watch:validation .
docker run --rm --network none newapi-watch:validation
```
