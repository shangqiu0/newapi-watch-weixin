# NewAPI Watch 实现计划

> 执行者使用 executing-plans 和 TDD 逐任务实现。本次用户已授权自主执行，无需阶段性等待批准。不使用子代理（未获明确授权）。

**目标：** 独立、单权限的 NewAPI 监控及企微查询服务。
**架构：** 采集客户端 → SQLite/规则 → 持久化通知队列；命令服务复用于 HTTP 与企微长连接。图表日报走同一数据服务。
**技术栈：** Python 3.12+ / FastAPI / httpx / aiosqlite / Pydantic / matplotlib / WeCom SDK。
**规格：** docs/superpowers/specs/2026-11-10-newapi-watch-design.md。

## 全局约束
单权限、零侵入；没有角色/登录/绑定。所有业务数据 DTO 白名单。服务密钥和验签仍必需。所有 HTTP 超时；单进程运行。

## 文件职责
config.py 配置；models.py 边界模型；client.py NewAPI 协议；store.py/schema.sql 持久化；monitor.py 采集和规则；notifications.py 出站通知；commands.py 命令；reports.py 图表日报；bot.py SDK入口；runtime.py 调度；api.py HTTP；demo.py 模拟服务；plugins.py 扩展；__main__.py CLI。

## 审查重点
分页中断不导致误删除；相同秒日志不漏；投递失败不丢且恢复有序；异常内容不泄漏凭证；重复/过期事件不造成重复告警；机器人断线不影响监控；日报边界按配置时区且标注数据完整性。

### 任务 1：配置和协议客户端
- [x] 先测试生产缺配置、分页全量/畸形/401/429/重试与脱敏。
- [x] 运行 pytest tests/test_client.py tests/test_config.py，确认 RED。
- [x] 实现配置、DTO 与 httpx 客户端，限页、固定结束时间、不自动重试主动测试。
- [x] 同命令 GREEN，记录结果。

### 任务 2：存储、采集和告警
- [x] 先测试首次异常、手动禁用、恢复、幂等、失败不推进游标、数据库重启与并发。
- [x] pytest tests/test_monitor.py：RED → 实现 schema、store、monitor → GREEN。
- [x] 指标保留原始 quota 整数，日志统计明确成功/错误记录而非最终用户成功率。

### 任务 3：通知和Webhook
- [x] 先测试业务errcode、429、队列重试/死信/顺序、HMAC/防重放/大小及服务密钥。
- [x] pytest tests/test_notifications.py tests/test_api.py：RED → 实现 → GREEN。

### 任务 4：命令、企微、图表日报
- [x] 先测试任意userid同权、消息去重、用户数据DTO、命令错误、PNG和DST日期窗口。
- [x] pytest tests/test_commands.py tests/test_reports.py tests/test_bot.py：RED → 实现 → GREEN。
- [x] SDK接入仅替换外部传输层，协议帧用真实SDK定义核验。

### 任务 5：调度、扩展与部署
- [x] 先测试定时任务故障隔离、日报去重和端到端告警/恢复/查询。
- [x] 实现 runtime、demo、插件注册、Docker/Compose、.env.example、README和CI。
- [x] 完整pytest、ruff、mypy、pip-audit和真实HTTP进程冒烟；保存结果到验证文档。

## 执行账本
见 docs/开发进度.md。当前没有Git仓库，不创建worktree或自动提交；使用当前目录与本地.venv。没有授权子代理，最终自审并说明局限。
