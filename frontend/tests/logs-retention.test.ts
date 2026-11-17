import { beforeEach, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus, {
  ElInputNumber,
  ElPagination,
  ElSegmented
} from "element-plus";
import Table from "@pureadmin/table";
vi.mock("@/api/console", () => ({ request: vi.fn() }));
import { request } from "@/api/console";
import { usePlatformStore } from "../src/store/modules/platform";
import Logs from "../src/views/console/logs.vue";

beforeEach(() => {
  setActivePinia(createPinia());
  usePlatformStore().currentId = "A";
  vi.clearAllMocks();
  vi.mocked(request).mockImplementation(async path => {
    const local = path.includes("live=false");
    return {
      items: [],
      total: local ? 45 : 123,
      page: 1,
      page_size: 20,
      source: local ? "local_newapi_logs" : "newapi_admin_api",
      retention: {
        enabled: true,
        logs_days: 7,
        scope: "local_only",
        next_check_at: 2000000000,
        last_run_at: 1999990000,
        last_deleted: 5
      }
    };
  });
});

it("请求日志区分上游实时和本地记录，并显示定时清理保留期", async () => {
  const view = mount(Logs, { global: { plugins: [ElementPlus, Table] } });
  await flushPromises();
  expect(view.text()).toContain("本地日志保留 7 天");
  expect(view.text()).toContain("定时清理设置");
  expect(view.text()).toContain("平台管理 API");
  view.findComponent(ElPagination).vm.$emit("update:current-page", 2);
  await flushPromises();
  view.findComponent(ElSegmented).vm.$emit("update:modelValue", "local");
  await flushPromises();
  const latest = vi.mocked(request).mock.calls.at(-1)?.[0] ?? "";
  expect(latest).toContain("live=false");
  expect(latest).toContain("page=1&");
  expect(view.text()).toContain("本地记录");
  expect(view.text()).not.toContain("平台管理 API");
  expect(view.findComponent(ElPagination).props("total")).toBe(45);
  view.unmount();
});

it("本地HTTP过滤使用正常分页，不套用上游继续扫描游标", async () => {
  const view = mount(Logs, { global: { plugins: [ElementPlus, Table] } });
  await flushPromises();
  view.findComponent(ElSegmented).vm.$emit("update:modelValue", "local");
  view.findComponent(ElInputNumber).vm.$emit("update:modelValue", 500);
  await flushPromises();
  expect(view.findComponent(ElPagination).exists()).toBe(true);
  expect(view.text()).not.toContain("继续查找");
  expect(vi.mocked(request).mock.calls.at(-1)?.[0]).toContain(
    "http_status=500"
  );
  view.unmount();
});

it("自动清理关闭时不得显示已启用的保留策略", async () => {
  vi.mocked(request).mockResolvedValue({
    items: [],
    total: 0,
    retention: { enabled: false, logs_days: 7, scope: "local_only" }
  });
  const view = mount(Logs, { global: { plugins: [ElementPlus, Table] } });
  await flushPromises();
  expect(view.text()).toContain("本地自动清理已关闭");
  expect(view.text()).not.toContain("本地日志保留 7 天");
  view.unmount();
});
