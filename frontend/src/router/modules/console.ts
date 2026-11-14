import Overview from "~icons/lucide/layout-dashboard";
import Server from "~icons/lucide/server";
import Logs from "~icons/lucide/scroll-text";
import Bell from "~icons/lucide/bell";
import Bot from "~icons/lucide/bot";
import Model from "~icons/lucide/plug";
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
    page("platforms", "Platforms", "平台管理", Server),
    page("policies", "Policies", "监控策略", Policies),
    page("channels", "Channels", "渠道状态", Server),
    page("logs", "Logs", "请求日志", Logs),
    page("users", "Users", "用户额度", Users),
    page("tasks", "QueryTasks", "查询任务", Policies),
    page("statistics", "Statistics", "按需统计", Reports),
    page("alerts", "Alerts", "告警记录", Bell),
    page("notifications", "Notifications", "通知投递", Bell),
    page("balances", "Balances", "余额监控", Wallet),
    page("reports", "Reports", "报告调度", Reports),
    page("bindings", "Bindings", "机器人与通知", Bot),
    page("model", "Model", "模型服务", Model),
    page("chat", "Chat", "问答工作区", Chat),
    page("audit", "Audit", "操作审计", Logs)
  ]
};
