import Overview from "~icons/lucide/layout-dashboard";
import Server from "~icons/lucide/server";
import Logs from "~icons/lucide/scroll-text";
import Bell from "~icons/lucide/bell";
import Bot from "~icons/lucide/bot";
import Settings from "~icons/lucide/settings-2";
import Chat from "~icons/lucide/messages-square";
import Users from "~icons/lucide/users";
import Wallet from "~icons/lucide/wallet";
import Policies from "~icons/lucide/list-checks";
import Reports from "~icons/lucide/calendar-clock";
const page = (path: string, name: string, title: string, icon: unknown) => ({
  path: "/console/" + path,
  name,
  component: () => import(`../../views/console/${path}.vue`),
  meta: { title, icon, keepAlive: false }
});
export default {
  path: "/",
  name: "Home",
  component: () => import("@/layout/index.vue"),
  redirect: "/console/overview",
  meta: { title: "监控工作台", icon: Overview, rank: 0 },
  children: [
    page("overview", "Overview", "监控工作台", Overview),
    {
      path: "/monitoring",
      name: "Monitoring",
      redirect: "/console/policies",
      meta: { title: "监控管理", icon: Policies },
      children: [
        page("policies", "Policies", "监控策略", Policies),
        page("balances", "Balances", "用户余额监控", Wallet),
        page("upstreams", "Upstreams", "上游账户", Wallet)
      ]
    },
    {
      path: "/data",
      name: "DataQueries",
      redirect: "/console/channels",
      meta: { title: "数据查询", icon: Logs },
      children: [
        page("channels", "Channels", "渠道状态", Server),
        page("users", "Users", "用户额度", Users),
        page("logs", "Logs", "请求日志", Logs),
        page("statistics", "Statistics", "按需统计", Reports),
        page("chat", "Chat", "问答工作区", Chat)
      ]
    },
    {
      path: "/alerts",
      name: "AlertCenter",
      redirect: "/console/alerts",
      meta: { title: "告警中心", icon: Bell },
      children: [
        page("alerts", "Alerts", "告警记录", Bell),
        page("notifications", "Notifications", "通知投递", Bell),
        page("reports", "Reports", "报告调度", Reports)
      ]
    },
    {
      path: "/configuration",
      name: "Configuration",
      redirect: "/console/settings",
      meta: { title: "配置管理", icon: Settings },
      children: [
        page("platforms", "Platforms", "平台管理", Server),
        page("settings", "PlatformSettings", "平台设置", Settings),
        page("bindings", "Bindings", "机器人与通知", Bot)
      ]
    },
    {
      path: "/console/tasks",
      name: "QueryTasks",
      redirect: { path: "/console/statistics", query: { tab: "tasks" } },
      meta: { showLink: false }
    },
    {
      path: "/console/currency",
      name: "Currency",
      redirect: { path: "/console/settings", query: { tab: "currency" } },
      meta: { showLink: false }
    },
    {
      path: "/console/model",
      name: "Model",
      redirect: { path: "/console/settings", query: { tab: "model" } },
      meta: { showLink: false }
    },
    {
      path: "/console/audit",
      name: "Audit",
      redirect: { path: "/console/settings", query: { tab: "records" } },
      meta: { showLink: false }
    }
  ]
};
