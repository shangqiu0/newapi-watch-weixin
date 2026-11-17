import { beforeEach, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus from "element-plus";
import Table from "@pureadmin/table";
vi.mock("@/api/console", () => ({
  request: vi.fn(),
  perform: async (fn: () => Promise<unknown>) => fn()
}));
import { request } from "@/api/console";
import { usePlatformStore } from "../src/store/modules/platform";
import Upstreams from "../src/views/console/upstreams.vue";
import DataManagement from "../src/components/Console/Settings/DataManagement.vue";
beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  usePlatformStore().currentId = "A";
});
it("上游账户分列显示今日和累计消费，未读取不伪造为零", async () => {
  vi.mocked(request).mockResolvedValue({
    items: [
      {
        id: "a",
        name: "供应商",
        base_url: "https://example.invalid",
        user_id: 7,
        currency: {},
        enabled: true,
        sample: {
          quota_display: "¥70.00",
          used_quota_display: "¥99.00",
          queried_at: 100
        },
        usage: {
          status: "ok",
          quota_display: "¥12.50",
          start_at: 10,
          end_at: 20,
          timezone: "Asia/Shanghai",
          stale: false
        }
      }
    ],
    total: 1
  });
  const view = mount(Upstreams, { global: { plugins: [ElementPlus, Table] } });
  await flushPromises();
  expect(view.text()).toContain("今日已用");
  expect(view.text()).toContain("¥12.50");
  expect(view.text()).toContain("累计已用");
  view.unmount();
});
it("数据管理明确只清理本地数据，预览不会触发删除", async () => {
  vi.mocked(request).mockResolvedValue({
    config: {
      enabled: true,
      logs_days: 30,
      history_days: 30,
      tasks_days: 1,
      audits_days: 30
    },
    counts: { logs: 10, notifications: 2, tasks: 3, audits: 4 },
    last_run: null
  });
  const view = mount(DataManagement, { global: { plugins: [ElementPlus] } });
  await flushPromises();
  expect(view.text()).toContain("不会删除上游");
  expect(view.text()).toContain("保留天数");
  expect(
    vi.mocked(request).mock.calls.some(([, method]) => method === "DELETE")
  ).toBe(false);
  view.unmount();
});

it("缩短保留期需确认，取消后不提交配置", async () => {
  const { ElInputNumber, ElMessageBox } = await import("element-plus");
  const confirmation = vi
    .spyOn(ElMessageBox, "confirm")
    .mockRejectedValueOnce("cancel");
  vi.mocked(request).mockImplementation(async path =>
    path === "/platforms"
      ? []
      : {
          config: {
            enabled: true,
            logs_days: 30,
            history_days: 30,
            tasks_days: 1,
            audits_days: 30
          },
          counts: {},
          last_run: null
        }
  );
  const view = mount(DataManagement, { global: { plugins: [ElementPlus] } });
  await flushPromises();
  view.findAllComponents(ElInputNumber)[0].vm.$emit("update:modelValue", 7);
  await flushPromises();
  await view.find("form").trigger("submit");
  await flushPromises();
  expect(confirmation).toHaveBeenCalled();
  expect(
    vi.mocked(request).mock.calls.some(([, method]) => method === "PATCH")
  ).toBe(false);
  view.unmount();
  confirmation.mockRestore();
});

it("上游列表支持原生列设置，隐藏列不改动后端账户", async () => {
  const { ElCheckboxGroup } = await import("element-plus");
  vi.mocked(request).mockResolvedValue({ items: [], total: 0 });
  const view = mount(Upstreams, { global: { plugins: [ElementPlus, Table] } });
  await flushPromises();
  await view
    .findAll("button")
    .find(button => button.text() === "列设置")
    .trigger("click");
  await flushPromises();
  const settings = view.findComponent(ElCheckboxGroup);
  expect(settings.exists()).toBe(true);
  const visible = settings.props("modelValue") as string[];
  settings.vm.$emit(
    "update:modelValue",
    visible.filter(value => !value.includes("今日已用"))
  );
  await flushPromises();
  expect(
    view.findAll("th").some(header => header.text().includes("今日已用"))
  ).toBe(false);
  expect(
    vi
      .mocked(request)
      .mock.calls.every(([, method]) => !method || method === "GET")
  ).toBe(true);
  view.unmount();
});
