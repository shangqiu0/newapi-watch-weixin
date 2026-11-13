# NewAPI Watch 管理台 · 前端重构开发文档（vue-pure-admin thin 路线）

> 2026-11-11 · v1.0
> 目标：将现有 `src/newapi_watch/web/`（原生 ES 模块，1114 行，7 个页面）重构为基于 **pure-admin-thin**（官方精简版，MIT，3.1k★）的 Vue 3 工程，风格按《设计提示词-管理台.md》v1.1 的 Token 通过主题适配层落地。
> 选型依据见《前端框架选型-管理台.md》v2。
> 后端（FastAPI `console_api.py`）**不改一行代码**——本文档所有工作都在前端侧。

---

## 0. TL;DR

```
clone pure-admin-thin → frontend/
pnpm install && pnpm dev          # 30 分钟内跑起来
删演示页（welcome/permission）     # 只留 login + error
接 /console/api（Cookie+CSRF 不变）
7 个业务页面 SFC 化
vite build → ../src/newapi_watch/web-dist → 打进 wheel
```

预计工期：M0 跑通 0.5 天 → M1 骨架迁移 1 天 → M2 七页业务 2 天 → M3 图表问答 1 天 → M4 打包回 Python 0.5 天，**约 5 个工作日**。

---

## 1. 现状盘点（迁移输入清单）

### 1.1 现有前端资产（全部在 `src/newapi_watch/web/`）

| 文件 | 行数 | 职责 | 重构去向 |
|---|---|---|---|
| `api.js` | 73 | fetch 封装：`/console/api` 前缀、Cookie same-origin、CSRF 头、401 全局事件、超时、中文时间工具 | `src/utils/http/` + `src/utils/format.ts`（逻辑原样移植） |
| `app.js` | 245 | 会话状态、平台切换、Tab 路由、generation 防竞态、抽屉、通知条 | `src/store/`（Pinia：session/platform）+ 路由守卫 |
| `views.js` | 88 | overview/channels/alerts/bindings/audit 五个纯函数视图 | `src/views/console/` 下 5 个 SFC |
| `interactive.js` | 106 | model/chat 两个交互视图（问答工作区） | `src/views/console/model.vue` `chat.vue` |
| `charts.js` | 76 | 手写 SVG 折线图（趋势几何） | ECharts（pure-admin 生态自带封装） |
| `components.js` | 62 | action/section/empty/loading/statusRow/rankList/dataTable | Element Plus 组件 + 本地二次封装 |
| `icons.js` | 12 | lucide 图标注入 | 保留 lucide（与 Element Plus 图标可共存，风格统一线描） |
| `theme.js` + `theme-init.js` | 34 | 主题切换 + 防闪烁 | pure-admin 内置主题系统（覆盖其变量为我们的 Token） |
| `console.css` | 360 | 全部样式（Token + 组件） | 弃用；样式由 Element Plus + Tailwind + 我们的 Token 适配层接管 |
| `index.html` | 58 | 登录视图 + 应用骨架 | pure-admin `login/index.vue` 替换 |

### 1.2 后端契约（`console_api.py`，重构期间冻结不动）

| 端点 | 方法 | 用途 |
|---|---|---|
| `/console/api/login` | POST | 登录（Set-Cookie `watch_session`，返回 `csrf_token`） |
| `/console/api/session` | GET | 会话探测（进入时校验） |
| `/console/api/logout` | POST | 登出 |
| `/console/api/platforms` | GET/POST | 平台列表/新增 |
| `/console/api/platforms/{id}` | PATCH | 编辑平台（空 secret 保留旧值） |
| `/console/api/platforms/{id}/overview` | GET | **核心聚合端点**：platform/channels/alerts/bindings/health/统计一次拿全 |
| `/console/api/platforms/{id}/bindings` | GET |
| `/console/api/platforms/{id}/bindings` (+/{binding_id}) | POST/PATCH | 绑定 bot/群 Webhook |
| `/console/api/platforms/{id}/queries` | POST | 问答工作区（LLM 取数回答） |
| `/console/api/platforms/{id}/model-checks` | POST | 模型连接测试 |
| `/console/api/platforms/{id}/checks` | POST | 平台连通测试 |
| `/health/live` | GET | 存活探针 |

**契约要点**（重构必须保持）：
1. 所有请求带 Cookie（`credentials: 'same-origin'`），写操作带 `X-CSRF-Token` 头
2. 401 时广播 `session-expired` → 跳登录页（pure-admin 里实现为 axios 响应拦截器 + 路由守卫）
3. 响应包络 `{code, data, message}`，业务只取 `data`；错误显示 `message`
4. `overview` 是单端点聚合——前端不需要并行多次请求，刷新逻辑照搬现有 generation 防竞态模式

### 1.3 页面清单（7 页 + 登录）

| Tab | 视图函数 | 内容要点 |
|---|---|---|
| overview | `overviewView` | KPI 条（渠道/告警计数）、健康状态、渠道消耗排行、趋势图 |
| channels | `channelsView` | 渠道快照表格（状态点+名称+余额+延迟），只读 |
| alerts | `alertsView` | 告警记录列表（状态/级别/时间） |
| bindings | `bindingsView` | 机器人与群 Webhook 绑定管理（增删改、启停） |
| model | `modelView` | LLM 配置表单 + 连接测试 |
| chat | `chatView` | 问答工作区（流式感对话 + 快捷提问） |
| audit | `auditView` | 操作审计（来源/类别/结果/耗时） |

---

## 2. M0 · 脚手架跑通（0.5 天）

```powershell
cd D:\work\newapi-mcpserver
git clone https://github.com/pure-admin/pure-admin-thin.git frontend
cd frontend
# Node 版本按仓库 .nvmrc（用 nvm-windows 切换：nvm install && nvm use）
pnpm install
pnpm dev            # 默认 http://your-newapi.example.com:8848
```

**跑通验收**：浏览器打开模板预览，登录页 → 进入布局（侧栏+顶栏+Tab）。

**立即做的事**：
1. 删 `frontend/.git`（脱离上游，后续用自己的仓库管理）
2. `package.json` 改名 `name: "newapi-watch-console"`
3. 开发代理：`vite.config.ts` 的 `server.proxy` 把 `/console/api` 和 `/health` 转发到 `http://127.0.0.1:8090`（本机跑着的 `newapi-watch console`）

```ts
// frontend/vite.config.ts 追加（platform: vite 内置 node 环境变量）
server: {
  port: 8848,
  proxy: {
    "/console/api": { target: "http://127.0.0.1:8090", changeOrigin: false },
    "/health": { target: "http://127.0.0.1:8090", changeOrigin: false },
  },
},
```

---

## 3. M1 · 删演示页 + 接后端（1 天）

### 3.1 删演示页清单（已核实 thin 版 src/views 内容）

| 路径 | 处置 |
|---|---|
| `src/views/welcome/` | **删除**（换成 overview） |
| `src/views/permission/` | **删除**（权限按钮演示，本项目单账号用不到） |
| `src/views/login/` | **保留改造**（接真实 `/console/api/login`） |
| `src/views/error/` | **保留**（404 等，路由兜底） |
| `src/router/modules/` 下对应 welcome/permission 路由 | **删除**，新增业务路由模块 |
| `src/api/` 下 user/login mock | **替换**为本项目 API 模块（见 3.3） |
| mock（若有） | 关闭/删除，全部走真实后端 |

thin 版本身已是精简结构（src: api/assets/components/config/directives/layout/plugins/router/store/style/utils/views），除 views 和路由模块外**其余全保留**——layout（侧栏/顶栏/Tab 骨架）正是我们要复用的核心资产。

### 3.2 路由规划

```
src/router/modules/console.ts   // 业务路由模块
├─ /login                       // 改造模板登录页
└─ / (layout)
   ├─ /console/overview         // 总览（首页，redirect / → 此页）
   ├─ /console/channels         // 渠道状态
   ├─ /console/alerts           // 告警记录
   ├─ /console/bindings         // 机器人与通知
   ├─ /console/model            // 模型服务
   ├─ /console/chat             // 问答工作区
   └─ /console/audit            // 操作审计
```

路由守卫改造：把模板默认的"用户名/密码 mock 校验"替换为——
1. 白名单 `/login`
2. 进入前 `GET /console/api/session` 探活：401 → 跳登录并带 redirect；成功 → csrf 存 store
3. 登录成功后 `POST /console/api/logout` 前置检查等逻辑照搬现有 `app.js` 的 `showLogin/enterSession`

### 3.3 HTTP 层（现有 api.js 的 axios 化，逻辑不变）

```ts
// src/utils/http/index.ts
const service = axios.create({ baseURL: "/console/api", timeout: 65000, withCredentials: true });
service.interceptors.request.use(cfg => {
  if (cfg.method !== "get") cfg.headers["X-CSRF-Token"] = useSessionStore().csrf;
  return cfg;
});
service.interceptors.response.use(
  resp => resp.data?.data,                       // 直接解包 {code,data,message}
  err => {
    if (err.response?.status === 401) { useSessionStore().expire(); router.push({ path: "/login", query: { redirect: ... } }); }
    ElMessage.error(err.response?.data?.message ?? "操作暂时失败，请重试");
    return Promise.reject(err);
  },
);
```

工具函数原样移植到 `src/utils/format.ts`：`timeCN`（中文时间）、`shortDate`（M月D日）、`number`（千分位）、`dateValue`。**中文时间格式是设计铁律，别用 dayjs 默认英文格式。**

### 3.4 Pinia store

```
src/store/modules/session.ts   // {authenticated, username, csrf, expire()}
src/store/modules/platform.ts   // {list, currentId, overview, generation, refresh(), select()}
```

`platform.ts` 直接把 `app.js` 里的 generation 递增 + AbortController 模式搬过来——这套防竞态逻辑是现有代码里最有价值的部分，Vue 化后依然需要（切平台时旧请求回来不能污染新视图）。

---

## 4. M2 · 七页业务 SFC 化（2 天）

### 4.1 组件映射表（旧 components.js → Element Plus/pure-admin）

| 旧组件 | 新实现 |
|---|---|
| `dataTable()` | pure-admin **PureTable**（模板内置，支持列配置/斑马纹/空态插槽） |
| `action()` | `el-button` 封装 `AButton`（loading/disabled 语义照搬） |
| `section()` | 页内 `el-card` + 自定义 header |
| `empty()` | `el-empty` + description + action 插槽 |
| `loading()` | `el-skeleton`（设计提示词：骨架屏优于转圈） |
| `statusRow()` | `el-descriptions` 或简单 flex 行 |
| `rankList()` | 自留小组件（条宽=占比，强调色），**不用饼图** |
| `state()` 状态点 | 自留 `StatusDot.vue`（6px 圆点+文字，四色语义，签名细节组件） |

### 4.2 各页实现要点

**overview.vue**：进入即 `platformStore.refresh()`；KPI 条用 4 格 `el-col`（数字等宽字体+对比小字，**不加迷你图**）；健康状态用 `StatusDot`；趋势图见 M3。
**channels.vue**：PureTable，列=状态点/名称/余额/响应时间/模型；数字列右对齐 + `tabular-nums`；行 hover 高亮。
**alerts.vue**：列表 + 级别 tag（critical=danger/warning=warn）+ 中文时间。
**bindings.vue**：bot 与群 Webhook 两组列表；新增/编辑用 `el-dialog` + 表单（secret 字段：GET 只回 has_secret，空值提交=保留旧值——这个语义在表单 hint 里写清楚）。
**model.vue**：base_url/api_key/model 三字段 + "连接测试"按钮（调 `/model-checks`，按钮 loading + 结果 ElMessage）。
**chat.vue**：消息气泡布局（沿用旧 CSS 语义重写）；发送调 `/queries`；历史按平台存 store（上限 100 条照搬）；快捷提问 chips。
**audit.vue**：PureTable 只读列表 + 时间/耗时等宽数字。

### 4.3 平台切换（顶栏）

现有 `platform-select` 改为顶栏 `el-select`；切换时 `platformStore.select(id)` 触发 generation++ + abort + refresh，Tab 页签内容全部从 `platformStore.overview` 响应式读取。

---

## 5. M3 · 图表与主题（1 天）

### 5.1 图表（替换手写 SVG）

用 pure-admin 生态的 **ECharts 封装**（或直接 vue-echarts 按需引入 LineChart）：
- 趋势图：折线+面积（12% 填充），线色读 CSS 变量（`getComputedStyle` 或 ECharts 主题注册），暗黑切换时 `chart.setOption` 更新
- 轴标签中文短日期（`M月D日`），tooltip 中文全时间——工具函数已有
- **暗黑模式 ECharts 需要注册两套主题**：监听 pure-admin 的主题 store，切换时 dispose+重建

### 5.2 主题适配层（设计提示词 v1.1 → Element Plus 变量）

pure-admin 用 Tailwind + 自有变量体系；Element Plus 有 `--el-color-primary` 系列。写一个 `styles/tokens.scss`：

```scss
// 亮色 :root / 暗色 .dark 两套（值来自设计提示词 v1.1 第 1 节）
:root {
  --el-color-primary: #237a73;
  --el-color-success: #16a34a;
  --el-color-warning: #986c22;
  --el-color-danger: #dc2626;
  --el-bg-color: #f6f7f7;
  --el-bg-color-overlay: #ffffff;
  --el-text-color-primary: #202a2b;
  --el-text-color-regular: #5b6668;
  --el-border-color: rgba(32,42,43,.08);
  --el-border-radius-base: 8px;
}
.dark {
  --el-color-primary: #4ab3a5;
  --el-color-success: #4cc38a;
  --el-color-warning: #d9a441;
  --el-color-danger: #e5484d;
  --el-bg-color: #121514;
  --el-bg-color-overlay: #181d1c;
  --el-text-color-primary: #e8ecea;
  --el-text-color-regular: #9faba9;
  --el-border-color: rgba(232,236,234,.08);
}
```

同时覆盖 pure-admin 侧栏/顶栏变量与其"主题配置"面板里的主色选项收敛为一个青绿。**验收标准**：切主题时全站（含图表）无残留旧色、无闪烁（`theme-init` 的防闪烁逻辑 pure-admin 已内置同类实现，确认即可）。

---

## 6. M4 · 打包回 Python wheel（0.5 天）

### 6.1 构建输出

```ts
// vite.config.ts
export default defineConfig({
  build: {
    outDir: "../src/newapi_watch/web-dist",
    emptyOutDir: true,
    assetsDir: "assets",
  },
});
```

### 6.2 Python 侧改动（仅三处，不动业务代码）

1. `pyproject.toml`：
   ```toml
   [tool.setuptools.package-data]
   newapi_watch = ["*.sql", "web-dist/*.html", "web-dist/assets/*", "web-dist/*.txt"]
   ```
2. `console_api.py` 静态挂载（278–284 行区域）：
   ```python
   web = Path(__file__).with_name("web-dist")
   app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets")
   @app.get("/", ...)
   async def index(): return FileResponse(web / "index.html", ...)
   # SPA 路由兜底（vue-router history 模式）：
   @app.get("/console/{rest:path}")  →  FileResponse(web / "index.html")
   ```
3. 旧 `web/` 目录：**先保留一轮**作为回滚备份，验收后删除并从 package-data 移除

### 6.3 构建链与 CI

- `frontend/package.json` scripts 保持 `pnpm build`；锁文件提交
- `.github/workflows/checks.yml` 增加 job：`pnpm --frozen-lockfile install && pnpm build`（产物随后被 Python 打包步骤使用）
- `Dockerfile` 改多阶段：`FROM node:22-slim AS fe` → `pnpm build` → `FROM python:3.13-slim` `COPY --from=fe /app/src/newapi_watch/web-dist ...`
- `--require-hashes` 是 Python 侧要求，前端用 pnpm 锁文件 + `pnpm audit` 等效保障；文档里注明这一映射关系

### 6.4 验收清单

- [ ] `newapi-watch console` 启动后，8090 打开的是 Vue 版管理台
- [ ] 登录/登出/401 跳转/CSRF 全链路正常
- [ ] 7 页全部可用，平台切换无竞态污染
- [ ] 暗黑/亮白切换：布局、表格、图表、弹窗无残留色
- [ ] 打包成 wheel 安装到干净环境，`web-dist` 随包分发，无 CDN 外联（DevTools Network 面板零外部请求）
- [ ] 移动端 760px：侧栏抽屉化、表格可横滚、无横向溢出
- [ ] 对照《设计提示词》第 12 条禁止清单逐项过（无紫色/无渐变发光/无 emoji 图标/中文时间/…）

---

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| pure-admin 升级困难（fork 后脱离 upstream） | M0 就删 `.git` 自管；组件问题去 full 版仓库查 issue（thin 版不收 issue，已核实） |
| Element Plus 样式与极简 Token 冲突 | 所有覆盖集中在 `tokens.scss` 一处，禁止散落 `!important` |
| vue-router history 模式刷新 404 | console_api 增加 SPA 兜底路由（6.2 已列） |
| ECharts 主题切换闪烁 | 监听主题 store，dispose 重建；或锁定随主题注册的两套 option |
| Node 版本差异（本机 Windows + .nvmrc） | nvm-windows 按 `.nvmrc` 安装；CI 固定 node 版本号 |
| 现有测试（test_console_v03 等）依赖旧 DOM 结构 | 它们测的是 HTTP 边界与后端逻辑，不测前端 DOM——迁移后应全绿；若有个别 fetch mock 断言路径变化，只改测试基址 |

## 8. 交付物

- `frontend/` Vue 3 工程（基于 pure-admin-thin，MIT 保留原 LICENSE）
- `src/newapi_watch/web-dist/`（构建产物，进 wheel）
- 本文档 + 更新后的 README 部署章节（console-init 流程不变，前端构建加入"开发者"一节）
