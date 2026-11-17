import { beforeEach, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus, { ElInput } from "element-plus";
import { request } from "@/api/console";
import { usePlatformStore } from "../src/store/modules/platform";
import type { Platform, Overview } from "../src/api/types";
import CurrencySettings from "../src/components/Console/Settings/CurrencySettings.vue";
import ModelSettings from "../src/components/Console/Settings/ModelSettings.vue";
vi.mock("@/api/console", () => ({
  request: vi.fn(),
  perform: async (fn: () => Promise<unknown>) => fn()
}));
const currency = {
  confirmed: true,
  quota_per_unit: "500000",
  usd_exchange_rate: "7.3",
  source: "manual" as const
};
beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  const store = usePlatformStore();
  store.currentId = "A";
  store.list = [
    {
      id: "A",
      name: "A",
      currency,
      llm: {
        enabled: false,
        base_url: "https://model.invalid",
        model: "configured-model",
        compatibility: "openai",
        has_api_key: true
      }
    } as Platform
  ];
  store.overview = { platform: store.list[0], model_status: null } as Overview;
  vi.spyOn(store, "load").mockResolvedValue(undefined);
});
it("金额分区保存只提交金额参数，不覆盖模型或监控配置", async () => {
  vi.mocked(request).mockResolvedValue({});
  const view = mount(CurrencySettings, { global: { plugins: [ElementPlus] } });
  await flushPromises();
  await view.find("form").trigger("submit");
  await flushPromises();
  const call = vi
    .mocked(request)
    .mock.calls.find(([, method]) => method === "PATCH");
  expect(call?.[0]).toBe("/platforms/A");
  expect(call?.[2]).toEqual({ currency });
  view.unmount();
});
it("模型分区保存空Key保留原值，提交字段不包含其他分区", async () => {
  vi.mocked(request).mockResolvedValue({});
  const view = mount(ModelSettings, { global: { plugins: [ElementPlus] } });
  await flushPromises();
  await view.find("form").trigger("submit");
  await flushPromises();
  const call = vi
    .mocked(request)
    .mock.calls.find(([, method]) => method === "PATCH");
  expect(call?.[0]).toBe("/platforms/A");
  expect(call?.[2]).toEqual({
    llm: {
      enabled: false,
      base_url: "https://model.invalid",
      model: "configured-model",
      compatibility: "openai",
      api_key: ""
    }
  });
  view.unmount();
});
it("当前编辑草稿不被迟到的同平台概览刷新覆盖", async () => {
  const store = usePlatformStore();
  const view = mount(ModelSettings, { global: { plugins: [ElementPlus] } });
  await flushPromises();
  const modelInput = view.findAllComponents(ElInput)[1];
  modelInput.vm.$emit("update:modelValue", "draft-model");
  await modelInput.find("input").trigger("input");
  await flushPromises();
  store.overview = {
    platform: {
      ...store.list[0],
      llm: { ...store.list[0].llm, model: "server-model" }
    },
    model_status: null
  } as Overview;
  await flushPromises();
  expect(modelInput.props("modelValue")).toBe("draft-model");
  view.unmount();
});

it("概览请求失败时仍使用已加载的平台模型配置，不以空默认值覆盖", async () => {
  usePlatformStore().overview = null;
  const view = mount(ModelSettings, { global: { plugins: [ElementPlus] } });
  await flushPromises();
  expect(view.findAllComponents(ElInput)[1].props("modelValue")).toBe(
    "configured-model"
  );
  view.unmount();
});
