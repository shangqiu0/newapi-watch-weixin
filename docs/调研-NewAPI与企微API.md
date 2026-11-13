> 历史资料提示（2026-11-10）：后续用户已确认所有参与者权限一致，不做多用户/角色平台。当前实现与修订结论请以 README.md、docs/实现说明与验证.md 和 docs/superpowers/specs/2026-11-10-newapi-watch-design.md 为准；以下原始记录保留备查。

# 调研笔记：NewAPI 管理 API 与企微机器人能力（已核实）

## NewAPI 内置事件通知（v2 新核实，来自源码 service/user_notify.go / webhook.go / channel.go）

- 触发事件：**渠道自动禁用**（DisableChannel，错误类型/状态码/Aho-Corasick 关键词判定）、**渠道自动恢复**（EnableChannel）、**渠道测试完成**、**上游模型更新**（NotifyUpstreamModelUpdateWatchers）
- 通知对象：root 默认收；其他管理员需 opt-in（ChannelNotifyEnabled）
- 通知通道：**邮件（默认）/ Webhook / Bark / Gotify**，无企微直推
- Webhook 格式：POST JSON `{Type, Title, Content, Values?, Timestamp}`；若配 secret，附 `X-Webhook-Signature`（HMAC-SHA256 over raw body, hex）；SSRF 防护客户端发送；非 2xx 视为失败
- ⚠️ payload **不含结构化 channel_id**，需从 Content（i18n 文本）解析或收事件后调渠道列表反查
- 自动禁用判定：`ShouldDisableChannel` = 全局开关开 && (IsChannelError || ShouldDisableByStatusCode || 命中 AutomaticDisableKeywords)；恢复判定：探测成功 && 当前 status=3 && 自动启用开关开

## NewAPI 渠道接口（全部 AdminAuth，router/channel-router.go）

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | /api/channel/ | 渠道列表（p, page_size, status, type, id_sort, tag_mode 过滤）|
| GET | /api/channel/:id | 单渠道详情 |
| GET | /api/channel/search | 搜索 |
| GET | /api/channel/test | 测试全部渠道（异步任务，返回 task_id；409=已在跑）|
| GET | /api/channel/test/:id | 测试单渠道 → `{"success","message","time","error_code"}` |
| POST | /api/channel/:id/status | 更新渠道状态 |
| POST | /api/channel/status/batch | 批量更新状态 |
| GET | /api/channel/update_balance/:id | 更新单渠道余额 |
| GET | /api/channel/update_balance | 更新全部余额 |
| POST | /api/channel/tag/disabled · /tag/enabled | 按标签禁用/启用 |
| DELETE | /api/channel/:id | 删除渠道 |
| PUT | /api/channel/ | 更新渠道 |
| GET | /api/channel/:id/vllm/status · /sglang/status | vLLM/SGLang 状态 |

权限分级：ChannelRead / ChannelOperate / ChannelWrite / ChannelSensitiveWrite（token scope 声明也注册了）。

## 渠道状态常量（common/constants.go）

```
ChannelStatusUnknown = 0  // 勿当启用
ChannelStatusEnabled = 1
ChannelStatusManuallyDisabled = 2
ChannelStatusAutoDisabled = 3   // ← 自动禁用是核心告警源
```

## 日志/数据接口（router/api-router.go）

| 方法 | 路径 | 权限 |
|------|------|------|
| GET | /api/log/ | Admin |
| GET | /api/log/search | Admin |
| GET | /api/log/stat | Admin |
| GET | /api/log/self(/search) | User |
| GET | /api/log/export | Admin |
| GET | /api/data/ | Admin（按日 quota 聚合）|
| GET | /api/data/users · /flow | Admin |
| GET | /api/data/self · /flow/self | User |

## 企微群机器人 Webhook

- `POST https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<UUID>`
- msgtype: text / markdown / markdown_v2 / image(base64+md5 ≤2MB) / news / file(media_id) / voice / template_card
- 限速 20 条/分钟；markdown ≤4096B；text ≤2048B
- text 可 mentioned_list / mentioned_mobile_list（支持 @all）
- 文件上传：`webhook/upload_media`，media_id 3 天有效

## 企微智能机器人（API 模式）

两种接入二选一（互斥）：
1. **长连接（推荐）**：wss://openws.work.weixin.qq.com，subscribe(aibot_subscribe, bot_id+secret)，无公网 IP、无加解密；ping 30s；单 bot 单连接
2. **回调 URL**：需公网 + Token/EncodingAESKey 加解密 + echostr 验证

消息 JSON 格式（非 XML）：msgid(去重) / aibotid / chatid(群) / chattype(single|group) / from.userid / response_url / msgtype(text|image|mixed|voice|file|video|stream) / text.content(群聊含@前缀)。
- 群聊只有 @机器人 才触发回调；私聊支持文本/语音(自动转文本)/图片
- 用户与同一机器人最多 3 条并发交互；语音转文本直接可用
- 回复：被动流式(reply_stream, stream.id, finish=true) / 模板卡片；或 POST response_url(1h 有效, 仅一次, markdown ≤20480B)
- 主动推送 aibot_send_msg：用户须先发过消息；限速 30/min·1000/h
- 欢迎消息/卡片更新须 5s 内；流式 10min 内完成
- 官方 SDK：Python `wecom-aibot-python-sdk`（MIT，WebSocket、reply_stream、send_message、模板卡片、文件 AES-256-CBC 解密）；Node `@wecom/aibot-node-sdk`

## 企微自建应用推送

- `POST https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=...`
- touser/toparty/totag（须在应用可见范围，否则 81013）；同成员 30/min·1000/h
- access_token：corpid+corpsecret 获取（/document/path/91039）
- text/markdown ≤2048B；支持模板卡片、图文

## 二开候选框架（v2 新增）

- **chatgpt-on-wechat / CowAgent**（MIT，Python 为主）：成熟多 IM 对话机器人框架。渠道：微信/企微（自建应用+Bot）/飞书/钉钉/Telegram/QQ/Discord/Slack，单实例多渠道并行。能力：Web 控制台、插件→Tool/Skill 体系（Skill Hub 一键装）、原生 MCP 集成、多 LLM（可连 NewAPI /v1）。企微自建应用渠道需公网 IP+回调(/wxcomapp, 9898)+可信 IP；文档 docs.cowagent.ai/channels/wecom
- 二开思路：写 newapi-monitor 插件注册命令（渠道状态/日报/图表），监控 cron 在插件内跑

## 同类项目

- li0on3/newapi-monitor：FastAPI+React+SQLite，外部探测、渠道/延迟/资源监控、企微(群+应用)+飞书+邮件告警、告警队列指数退避+死信、SSO、双语、Docker、MIT。**告警设计可借鉴**，无对话机器人。
- yigehaozi/relay-status-monitor：Next.js14+PG，余额/延迟/测速、轻重量分层采集、飞书告警。**采集分层可借鉴**。
- Ruanyuxi1337/newapi-operator：Go 的 NewAPI 全管理面 MCP server。
