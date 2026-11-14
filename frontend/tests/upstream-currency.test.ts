import { beforeEach, describe, it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus from "element-plus";
import Table from "@pureadmin/table";
vi.mock("@/api/console", () => ({
  request: vi.fn(),
  perform: async (task: () => Promise<unknown>) => task()
}));
import { request } from "@/api/console";
import CurrencyEditor from "../src/components/Console/CurrencyEditor.vue";
import Upstreams from "../src/views/console/upstreams.vue";
import { usePlatformStore } from "../src/store/modules/platform";

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});
describe("人民币与上游账户", () => {
  it("读取候选参数只更新编辑值，不自动保存配置", async () => {
    vi.mocked(request).mockResolvedValue({
      confirmed: true,
      quota_per_unit: "500000",
      usd_exchange_rate: "7.3",
      source: "status"
    });
    const view = mount(CurrencyEditor, {
      props: {
        modelValue: {
          confirmed: false,
          quota_per_unit: "500000",
          usd_exchange_rate: "1",
          source: "manual"
        },
        previewPath: "/platforms/A/currency-previews"
      },
      global: { plugins: [ElementPlus, Table] }
    });
    await view
      .findAll("button")
      .find(b => b.text().includes("读取平台参数"))
      .trigger("click");
    await flushPromises();
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith(
      "/platforms/A/currency-previews",
      "POST"
    );
    expect(view.emitted("update:modelValue")?.[0]?.[0]).toMatchObject({
      usd_exchange_rate: "7.3",
      confirmed: true
    });
    view.unmount();
  });
  it("仅列本平台账户，页面不自动实时查询且显示后端人民币", async () => {
    usePlatformStore().currentId = "A";
    vi.mocked(request).mockResolvedValue({
      items: [
        {
          id: "a".repeat(32),
          name: "上游甲",
          base_url: "https://up.invalid",
          user_id: 7,
          enabled: true,
          monitor_enabled: false,
          query_binding_ids: [],
          notify_binding_ids: [],
          currency: {
            confirmed: true,
            quota_per_unit: "500000",
            usd_exchange_rate: "7.3",
            source: "status"
          },
          threshold_cny: "10",
          recovery_threshold_cny: null,
          poll_seconds: 300,
          cooldown_seconds: 1800,
          notify_recovery: true,
          sample: {
            quota: 500000,
            quota_display: "¥7.30",
            used_quota_display: "¥14.60",
            queried_at: 1794627249
          },
          error: ""
        }
      ],
      total: 1,
      page: 1,
      page_size: 20
    });
    const view = mount(Upstreams, {
      global: { plugins: [ElementPlus, Table] }
    });
    await flushPromises();
    expect(view.text()).toContain("¥7.30");
    expect(view.text()).toContain("¥14.60");
    expect(
      vi
        .mocked(request)
        .mock.calls.every(c => !c[0].includes("balance-queries"))
    ).toBe(true);
    expect(vi.mocked(request).mock.calls[0][0]).toContain(
      "/platforms/A/upstream-accounts"
    );
    view.unmount();
  });
});

it("切换平台后不把旧平台的金额候选参数写到新平台编辑框", async () => {
  let finish: (value: unknown) => void;
  vi.mocked(request).mockImplementation(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  const view = mount(CurrencyEditor, {
    props: {
      modelValue: {
        confirmed: false,
        quota_per_unit: "500000",
        usd_exchange_rate: "1",
        source: "manual"
      },
      previewPath: "/platforms/A/currency-previews"
    },
    global: { plugins: [ElementPlus] }
  });
  await view
    .findAll("button")
    .find(b => b.text().includes("读取平台参数"))
    .trigger("click");
  await view.setProps({ previewPath: "/platforms/B/currency-previews" });
  finish({
    confirmed: true,
    quota_per_unit: "1000000",
    usd_exchange_rate: "5",
    source: "status"
  });
  await flushPromises();
  expect(view.emitted("update:modelValue")).toBeUndefined();
  view.unmount();
});
