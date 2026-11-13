import { it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus, { ElForm } from "element-plus";
vi.mock("@/api/console", () => ({
  request: vi.fn(),
  perform: async (task: () => Promise<unknown>) => task()
}));
import { request } from "@/api/console";
import PlatformEditor from "../src/components/Console/PlatformEditor.vue";
it("编辑平台只提交允许的连接配置，不混入id/状态/llm或旧采集间隔", async () => {
  setActivePinia(createPinia());
  const platform = {
    id: "A",
    name: "真实平台",
    newapi_url: "https://a.invalid",
    newapi_user_id: 7,
    timezone: "Asia/Shanghai",
    enabled: false,
    poll_seconds: 30,
    log_seconds: 120,
    has_newapi_token: true,
    llm: {
      enabled: false,
      compatibility: "auto" as const,
      base_url: "",
      model: "",
      has_api_key: false
    }
  };
  vi.mocked(request).mockImplementation(async (path, method) =>
    method === "PATCH" ? platform : path === "/platforms" ? [platform] : {}
  );
  const view = mount(PlatformEditor, {
    props: { modelValue: false, platform },
    global: { plugins: [ElementPlus] }
  });
  await view.setProps({ modelValue: true });
  await flushPromises();
  await view.findComponent(ElForm).trigger("submit");
  await flushPromises();
  const payload = vi
    .mocked(request)
    .mock.calls.find(call => call[1] === "PATCH")[2];
  expect(payload).toEqual({
    name: "真实平台",
    newapi_token: "",
    newapi_user_id: 7,
    timezone: "Asia/Shanghai",
    enabled: false
  });
  view.unmount();
});

it("从平台管理编辑非当前平台后保持原选择，并阻止重复提交", async () => {
  localStorage.clear();
  setActivePinia(createPinia());
  const { usePlatformStore } = await import("../src/store/modules/platform");
  const store = usePlatformStore();
  store.currentId = "B";
  const target = {
    id: "A",
    name: "A",
    newapi_url: "https://a.invalid",
    newapi_user_id: 1,
    timezone: "UTC",
    enabled: false,
    poll_seconds: 30,
    log_seconds: 120,
    has_newapi_token: true,
    llm: {
      enabled: false,
      compatibility: "auto" as const,
      base_url: "",
      model: "",
      has_api_key: false
    }
  };
  let resolveSave: (value: unknown) => void;
  vi.mocked(request)
    .mockClear()
    .mockImplementation((path, method) =>
      method === "PATCH"
        ? new Promise(resolve => {
            resolveSave = resolve;
          })
        : Promise.resolve(
            path === "/platforms" ? [target, { ...target, id: "B" }] : {}
          )
    );
  const view = mount(PlatformEditor, {
    props: { modelValue: false, platform: target },
    global: { plugins: [ElementPlus] }
  });
  await view.setProps({ modelValue: true });
  await flushPromises();
  await view.findComponent(ElForm).trigger("submit");
  await view.findComponent(ElForm).trigger("submit");
  expect(
    vi.mocked(request).mock.calls.filter(c => c[1] === "PATCH")
  ).toHaveLength(1);
  resolveSave(target);
  await flushPromises();
  expect(store.currentId).toBe("B");
  view.unmount();
});
