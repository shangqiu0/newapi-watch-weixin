import { beforeEach, describe, it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus from "element-plus";
vi.mock("@/api/console", () => ({
  request: vi.fn(),
  perform: async (task: () => Promise<unknown>) => task()
}));
import { request } from "@/api/console";
import PolicyEditor from "../src/views/console/policies.vue";
import { usePlatformStore } from "../src/store/modules/platform";

function fixture() {
  const common = {
    binding_ids: [],
    confirmations: 1,
    cooldown_seconds: 300,
    notify_recovery: true,
    mention_all: false
  };
  return {
    requests: { min_interval_seconds: 1.2, rate_limit_cooldown_seconds: 60 },
    channels: {
      ...common,
      enabled: true,
      poll_seconds: 60,
      channel_ids: [],
      notify_missing: true
    },
    source: { ...common, enabled: true },
    errors: {
      ...common,
      enabled: false,
      poll_seconds: 120,
      initial_lookback_seconds: 0,
      window_seconds: 300,
      overlap_seconds: 30,
      status_codes: [400, 500],
      channel_ids: [],
      models: [],
      error_codes: [],
      template: "渠道异常 {channel_id}"
    },
    balances: [],
    balance_poll_seconds: 300,
    daily: {
      enabled: false,
      hour: 9,
      minute: 0,
      weekday: 0,
      with_image: true,
      binding_ids: []
    },
    weekly: {
      enabled: false,
      hour: 9,
      minute: 0,
      weekday: 0,
      with_image: true,
      binding_ids: []
    }
  };
}
beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});
describe("真实组件策略编辑", () => {
  it("切换启停后，点击保存才将本平台完整策略提交到后端", async () => {
    const platform = usePlatformStore();
    platform.currentId = "platform-A";
    const submissions: unknown[] = [];
    vi.mocked(request).mockImplementation(async (_path, method, data) => {
      if (method === "PUT") {
        submissions.push(data);
        return data;
      }
      return { config: fixture(), samples: [], errors: {} };
    });
    const view = mount(PolicyEditor, { global: { plugins: [ElementPlus] } });
    await flushPromises();
    await view.get('[aria-label="渠道状态策略启用"]').trigger("click");
    expect(submissions).toHaveLength(0);
    const save = view
      .findAll("button")
      .find(button => button.text() === "保存策略");
    await save.trigger("click");
    await flushPromises();
    expect(submissions).toHaveLength(1);
    expect(submissions[0]).toMatchObject({
      channels: { enabled: false, poll_seconds: 60 },
      errors: { enabled: false, initial_lookback_seconds: 0 }
    });
    const put = vi.mocked(request).mock.calls.find(call => call[1] === "PUT");
    expect(put[0]).toBe("/platforms/platform-A/monitoring");
    view.unmount();
  });
  it("读取失败显示错误及禁止保存，不显示可执行的空配置", async () => {
    usePlatformStore().currentId = "platform-A";
    vi.mocked(request).mockRejectedValue(new Error("接口不可用"));
    const view = mount(PolicyEditor, { global: { plugins: [ElementPlus] } });
    await flushPromises();
    expect(view.text()).toContain("接口不可用");
    const save = view
      .findAll("button")
      .find(button => button.text() === "保存策略");
    expect(save.attributes("disabled")).toBeDefined();
    view.unmount();
  });
});
