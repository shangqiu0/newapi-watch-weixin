# NewAPI Watch Console — pure-admin-thin 本地开发

本前端基于官方 pure-admin/pure-admin-thin 6.2.0 的布局、页签与组件系统，保留 `LICENSE`。与上游的区别：无 Mock、无 JWT/刷新 Token、无多角色示例；通过现有 FastAPI Cookie + CSRF 管理接口工作。

## 运行

Node 版本见 `.nvmrc`（22.20.0）；包管理器固定 pnpm 10.33.0。

```powershell
pnpm install --frozen-lockfile
pnpm dev
```

开发地址 `http://127.0.0.1:8848/`；`/console/api` 与 `/health` 默认代理到本机 8090。只绑定 127.0.0.1，不开放外网。可在不提交的 `.env.development.local` 中配置 `VITE_CONSOLE_TARGET` 指向隔离测试后端。

后端在项目根目录用源码启动：

```powershell
.\.venv-dev\Scripts\python.exe -m newapi_watch console --control-dir data/console --host 127.0.0.1 --port 8090 --insecure-cookie
```

当前两个本地服务已经启动时，不要重复启动，尤其不要建立重复的机器人连接。修改依赖前先停止相应开发服务；不要在它运行时原地同步 Python 环境。

## 目录

- `src/api/`：包络解包、CSRF、DTO 类型
- `src/store/modules/session.ts`：只在内存保存会话状态与 CSRF，浏览器持有 HttpOnly Cookie
- `src/store/modules/platform.ts`：平台切换、取消与 generation 防竞态
- `src/store/modules/chat.ts`：按平台隔离对话、注销清理
- `src/composables/`：查询生命周期与监控设置
- `src/views/console/`：15 个业务页面，新增平台管理、监控策略、通知投递及按需统计；首页以策略检查为中心
- `src/components/Console/`：状态点、平台编辑、布局标题、ECharts 与主题适配
- `src/styles/tokens.scss`：业务主题与 Element Plus/pure-admin 适配；不散落颜色体系
- `src/layout/`：复用并适配的上游布局/侧栏/页签
- `tests/`：Vitest，使用隔离数据，不使用真实密钥

监控配置保存只热更新本平台策略，不重连机器人；平台连接配置修改才重新加载当前平台。配置入口：监控策略（渠道/错误/来源）、余额监控（逐用户）、报告调度、机器人与通知。

## 验证

```powershell
pnpm typecheck
pnpm test
pnpm audit --prod
pnpm build
```

`build` 只生成 `../src/newapi_watch/web-dist`，用于本地静态入口与测试；不是 wheel/Docker 发布。日常样式与业务开发使用 `dev` 热更新，不需要反复构建。Vue 类型检查器固定 3.1.2：上游升级到 3.3.14 后对现有 reactive 表单产生不正确的 `.value` 检查错误，已用当前组合通过 typecheck、测试和构建。

不保留原生 `src/newapi_watch/web/` 前端；源码入口只有这里。首次克隆如要由 Python 8090 提供静态页面，需先执行一次前端 `build`；未构建时后端给出可操作的 503 提示而不是难以定位的 500。
