import { beforeEach, describe, it, expect, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import ElementPlus, { ElPagination } from "element-plus";
import Table from "@pureadmin/table";
vi.mock("@/api/console", () => ({
  request: vi.fn(),
  perform: async (fn: () => Promise<unknown>) => fn()
}));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
import { request } from "@/api/console";
import { usePlatformStore } from "../src/store/modules/platform";
import Alerts from "../src/views/console/alerts.vue";
import Notifications from "../src/views/console/notifications.vue";
import Channels from "../src/views/console/channels.vue";
import Logs from "../src/views/console/logs.vue";
import Users from "../src/views/console/users.vue";
import Audits from "../src/components/Console/Settings/CommandRecords.vue";

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(request).mockReset();
  usePlatformStore().currentId = "platform-A";
});
describe.each([
  ["告警", Alerts],
  ["通知", Notifications],
  ["渠道", Channels],
  ["日志", Logs],
  ["用户", Users],
  ["审计", Audits]
])("%s列表真实分页组件", (_name, component) => {
  it("第2页请求期间不归零总数、不自动跳回第1页，并可选择每页条数", async () => {
    let resolvePage: (value: unknown) => void;
    vi.mocked(request).mockImplementation(path =>
      path.includes("page=2")
        ? new Promise(resolve => {
            resolvePage = resolve;
          })
        : Promise.resolve({ items: [], total: 104, page: 1, page_size: 20 })
    );
    const view = mount(component, {
      global: {
        plugins: [ElementPlus, Table],
        mocks: { $router: { push: vi.fn() } }
      }
    });
    await flushPromises();
    const pager = view.findComponent(ElPagination);
    expect(pager.props("total")).toBe(104);
    const second = pager.findAll("li.number").find(item => item.text() === "2");
    await second.trigger("click");
    await nextTick();
    await nextTick();
    expect(pager.props("currentPage")).toBe(2);
    expect(pager.props("total")).toBe(104);
    resolvePage({ items: [], total: 104, page: 2, page_size: 20 });
    await flushPromises();
    expect(pager.props("currentPage")).toBe(2);
    expect(pager.props("pageSizes")).toContain(50);
    expect(pager.props("layout")).toContain("sizes");
    pager.vm.$emit("update:page-size", 50);
    await flushPromises();
    expect(pager.props("pageSize")).toBe(50);
    expect(pager.props("currentPage")).toBe(1);
    expect(
      vi
        .mocked(request)
        .mock.calls.some(call => call[0].includes("page_size=50"))
    ).toBe(true);
    view.unmount();
  });
});
