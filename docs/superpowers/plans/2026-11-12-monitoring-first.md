# 监控策略优先 实现计划

> 面向 AI 代理：使用 executing-plans 在当前会话执行，用户已明确要求按已确认方案实现，不再等待重复确认。无Git仓库、不使用未授权子代理。

**目标：** 替换Console常驻采集为策略检查，提供真实监控工作台。
**架构：** monitoring模型/仓库/策略引擎分离，Fleet将配置注入Runtime，沿用outbox。DataQueries与内存聚合为独立手动查询。
**技术栈：** 现有Python3.12/FastAPI/httpx/SQLite与Vue3/pure-admin/Element Plus。

## 任务1：配置、最小状态和策略检查引擎
- [x] tests/test_policy_monitoring.py先写失败测试：无日志入库、首次基线、窗口失败不推进、手动禁用、连续确认与恢复、余额失败未知、单次共享、持久化冷却。
- [x] monitoring/models.py增加渠道/来源/窗口/目标配置；004.sql增加monitor_checks和monitor_states；migrations.py加法升级。
- [x] monitoring/repository.py负责最小状态/outbox原子写；engine.py负责有界调度/来源失败；checks.py处理渠道与余额；service.py复用错误事件与报表。
- [x] Runtime.open添加policy模式；Fleet构造服务后再启动任务，不启动旧channels/logs/errors采集。
- [x] 运行python -m pytest tests/test_policy_monitoring.py tests/test_monitoring_v05.py并检查通过。

## 任务2：查询/报表与业务存储解耦
- [x] tests/test_live_data_v05.py增加查询不写logs与缓存/完整失败测试，先RED。
- [x] memory_analytics.py内存聚合；data_queries.py返回短期缓存日志，console_extensions.py日志分页不依赖SQLite；默认查询上游，保留显式legacy local模式。
- [x] 运行对应测试并回归现有统计数值/范围。

## 任务3：管理API与监控工作台
- [x] tests/test_console_v05.py新增监控状态/复查、鉴权、目标归属/无效输入测试，先RED。
- [x] monitoring API返回执行状态和队列；POST monitor-runs幂等复查，不无意发送测试消息。保存策略只热更新策略，不重连机器人。
- [x] frontend types/composables/overview/policies/alerts/balances/reports改造，保留pure-admin布局与tokens。
- [x] Vitest组件验证未知/禁用/下一次检查与保存动作；pnpm test/typecheck。

## 任务4：本地验收与交付
- [x] 全量pytest、ruff、mypy、Vitest、typecheck；不发布构建。
- [ ] Browser桌面/移动视觉验收：工具安全策略拒绝本机URL，停止且未绕过；已完成真实Vue组件交互、API与TCP进程的替代验证，不等同视觉验收。
- [x] 备份后本地源服务重启使变更生效，完整性检查.env/master.key，记录结果与已知限制。

## 审查重点
完整分页失败原子性、真实上游429/时间预算、初始化不重放、检查失败不恢复业务异常、跨平台目标、定时与手动竞态、任务取消/重启。
