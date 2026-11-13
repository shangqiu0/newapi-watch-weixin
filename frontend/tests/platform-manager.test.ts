import { beforeEach, it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus from "element-plus";
import Table from "@pureadmin/table";
import type { Platform } from "../src/api/types";
vi.mock("@/api/console", () => ({
  request: vi.fn(),
  perform: async (fn: () => Promise<unknown>) => {
    try {
      await fn();
    } catch {}
  }
}));
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
import { request } from "@/api/console";
import { usePlatformStore } from "../src/store/modules/platform";
import Platforms from "../src/views/console/platforms.vue";
import DeleteDialog from "../src/components/Console/PlatformDeleteDialog.vue";
const platform = (id: string): Platform => ({
  id,
  name: "平台" + id,
  newapi_url: `https://${id.toLowerCase()}.invalid`,
  newapi_user_id: 1,
  timezone: "UTC",
  enabled: false,
  poll_seconds: 30,
  log_seconds: 120,
  has_newapi_token: true,
  revision: 1,
  llm: {
    enabled: false,
    compatibility: "auto",
    base_url: "",
    model: "",
    has_api_key: false
  }
});
const options = {
  global: {
    plugins: [ElementPlus, Table],
    mocks: { $router: { push: vi.fn() } }
  }
};
beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(request).mockReset();
});
it("没有平台时也可打开平台管理并新增，不被平台空状态挡住", async () => {
  const view = mount(Platforms, options);
  await flushPromises();
  expect(view.text()).toContain("平台管理");
  const add = view.findAll("button").find(b => b.text() === "新增平台");
  expect(add.exists()).toBe(true);
  await add.trigger("click");
  await flushPromises();
  expect(view.text()).toContain("新增监控平台");
  view.unmount();
});
it("每个平台都有明确编辑和删除入口，编辑显示原配置", async () => {
  const store = usePlatformStore();
  store.list = [platform("A"), platform("B")];
  store.currentId = "A";
  const view = mount(Platforms, options);
  await flushPromises();
  const row = view.findAll("tr").find(r => r.text().includes("平台B"));
  const edit = row.findAll("button").find(b => b.text() === "编辑");
  const remove = row.findAll("button").find(b => b.text() === "删除");
  expect(edit.exists() && remove.exists()).toBe(true);
  await edit.trigger("click");
  await flushPromises();
  expect(view.find('input[aria-label="平台名称"]').element.value).toBe("平台B");
  expect(
    view.find('input[aria-label="NewAPI 地址"]').attributes("disabled")
  ).toBeDefined();
  view.unmount();
});
it("删除须匹配平台名，确认后只发一次请求并保持其他平台", async () => {
  const store = usePlatformStore();
  store.list = [platform("A"), platform("B")];
  store.currentId = "B";
  let finish: (value: unknown) => void;
  vi.mocked(request).mockImplementation(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  const view = mount(DeleteDialog, {
    ...options,
    props: { modelValue: false, platform: platform("A") }
  });
  await view.setProps({ modelValue: true });
  await flushPromises();
  const button = () =>
    view.findAll("button").find(b => b.text() === "确认删除");
  expect(button().attributes("disabled")).toBeDefined();
  await view
    .get('input[aria-label="输入平台名称确认删除"]')
    .setValue("错误名称");
  expect(button().attributes("disabled")).toBeDefined();
  await view.get('input[aria-label="输入平台名称确认删除"]').setValue("平台A");
  await button().trigger("click");
  await button().trigger("click");
  expect(
    vi.mocked(request).mock.calls.filter(c => c[1] === "DELETE")
  ).toHaveLength(1);
  finish({ deleted: true, history_retained: true });
  await flushPromises();
  expect(store.list.map(p => p.id)).toEqual(["B"]);
  expect(store.currentId).toBe("B");
  expect(view.emitted("update:modelValue").at(-1)).toEqual([false]);
  view.unmount();
});
it("删除失败保留对话框与列表，显示可重试错误", async () => {
  const store = usePlatformStore();
  store.list = [platform("A")];
  store.currentId = "A";
  vi.mocked(request).mockRejectedValue(new Error("平台版本变化，请刷新"));
  const view = mount(DeleteDialog, {
    ...options,
    props: { modelValue: false, platform: platform("A") }
  });
  await view.setProps({ modelValue: true });
  await flushPromises();
  await view.get('input[aria-label="输入平台名称确认删除"]').setValue("平台A");
  await view
    .findAll("button")
    .find(b => b.text() === "确认删除")
    .trigger("click");
  await flushPromises();
  expect(view.text()).toContain("平台版本变化，请刷新");
  expect(store.list).toHaveLength(1);
  expect(view.emitted("update:modelValue")).toBeUndefined();
  view.unmount();
});
it("取消删除不请求后端", async () => {
  const view = mount(DeleteDialog, {
    ...options,
    props: { modelValue: false, platform: platform("A") }
  });
  await view.setProps({ modelValue: true });
  await flushPromises();
  await view
    .findAll("button")
    .find(b => b.text() === "取消")
    .trigger("click");
  expect(request).not.toHaveBeenCalled();
  expect(view.emitted("update:modelValue").at(-1)).toEqual([false]);
  view.unmount();
});

it("删除最后一个平台后管理页仍显示新增入口，不需要重新登录", async () => {
  const store = usePlatformStore();
  store.list = [platform("A")];
  store.currentId = "A";
  vi.mocked(request).mockResolvedValue({
    deleted: true,
    history_retained: true
  });
  const view = mount(Platforms, options);
  await flushPromises();
  await view
    .findAll("button")
    .find(b => b.text() === "删除")
    .trigger("click");
  await flushPromises();
  await view.get('input[aria-label="输入平台名称确认删除"]').setValue("平台A");
  await view
    .findAll("button")
    .find(b => b.text() === "确认删除")
    .trigger("click");
  await flushPromises();
  expect(store.currentId).toBe("");
  expect(store.list).toEqual([]);
  expect(view.text()).toContain("接入第一个平台");
  expect(view.findAll("button").some(b => b.text() === "新增平台")).toBe(true);
  view.unmount();
});
