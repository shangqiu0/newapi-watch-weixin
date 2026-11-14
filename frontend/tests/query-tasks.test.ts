import { beforeEach, it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus from "element-plus";
import Table from "@pureadmin/table";
vi.mock("@/api/console", () => ({
  request: vi.fn(),
  perform: async (fn: () => Promise<unknown>) => fn()
}));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("../src/components/Console/TrendChart.vue", () => ({
  default: { template: "<div />" }
}));
import { request } from "@/api/console";
import { usePlatformStore } from "../src/store/modules/platform";
import QueryTasks from "../src/views/console/tasks.vue";
import Statistics from "../src/views/console/statistics.vue";
const fixture = {
  id: "q_" + "a".repeat(32),
  status: "paused",
  query: {
    start: "2026-11-12",
    end: "2026-11-12",
    dimension: "model",
    metric: "quota"
  },
  progress: { records_read: 500, total_requests: 5, restarts: 1 },
  result: null,
  error: "平台限流，进度已保存"
};
beforeEach(() => {
  setActivePinia(createPinia());
  usePlatformStore().currentId = "A";
  vi.clearAllMocks();
});
it("任务页显示真实进度和暂停原因，继续操作只针对当前平台", async () => {
  const actions: string[] = [];
  vi.mocked(request).mockImplementation(async (path, method) => {
    if (method === "POST") {
      actions.push(path);
      return fixture;
    }
    return { items: [fixture], total: 1, page: 1, page_size: 20 };
  });
  const view = mount(QueryTasks, { global: { plugins: [ElementPlus, Table] } });
  await flushPromises();
  expect(view.text()).toContain("500");
  expect(view.text()).toContain("平台限流");
  expect(view.text()).not.toContain("已投递");
  const retry = view.findAll("button").find(b => b.text() === "继续任务");
  expect(retry).toBeDefined();
  await retry.trigger("click");
  await flushPromises();
  expect(actions).toEqual([`/platforms/A/query-tasks/${fixture.id}/retries`]);
  view.unmount();
});
it("统计受理中不展示零用量或假装已完成", async () => {
  vi.mocked(request).mockResolvedValue({
    pending: true,
    task: fixture,
    limitation: "后台汇总中"
  });
  const view = mount(Statistics, { global: { plugins: [ElementPlus, Table] } });
  await flushPromises();
  expect(view.text()).toContain("查询任务已受理");
  expect(view.text()).toContain(fixture.id);
  expect(view.text()).not.toContain("0 quota");
  view.unmount();
});
it("小时汇总未提供的错误数不能显示成零", async () => {
  vi.mocked(request).mockResolvedValue({
    summary: { quota: 10 },
    ranking: [
      { key: "m", label: "m", quota: 10, error_records: 0, success_records: 2 }
    ],
    series: [],
    coverage: { complete: false },
    timezone: "Asia/Shanghai",
    source: "newapi_hourly_aggregate",
    unavailable_metrics: ["error_records"],
    limitation: "仅为已落盘数据"
  });
  const view = mount(Statistics, { global: { plugins: [ElementPlus, Table] } });
  await flushPromises();
  expect(view.text()).toContain("未提供");
  expect(view.text()).toContain("已落盘");
  view.unmount();
});
