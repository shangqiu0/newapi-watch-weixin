import { beforeEach, it, expect, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import ElementPlus from "element-plus";
vi.mock("@/api/console", () => ({
  request: vi.fn(),
  perform: async (fn: () => Promise<unknown>) => fn()
}));
import { request } from "@/api/console";
import { usePlatformStore } from "../src/store/modules/platform";
import { useChatStore } from "../src/store/modules/chat";
import Chat from "../src/views/console/chat.vue";
import type { Platform } from "../src/api/types";
beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(request).mockReset();
});
it("删除平台后，原平台迟到的聊天回答不能重新创建已删除的历史", async () => {
  const platform = usePlatformStore(),
    chat = useChatStore();
  platform.list = ["A", "B"].map(
    id => ({ id, name: id, enabled: true, llm: { enabled: false } }) as Platform
  );
  platform.currentId = "A";
  chat.conversation("A").draft = "question";
  let resolve: (value: unknown) => void;
  vi.mocked(request).mockImplementation(
    () =>
      new Promise(done => {
        resolve = done;
      })
  );
  const view = mount(Chat, { global: { plugins: [ElementPlus] } });
  await view.get("form").trigger("submit");
  expect(vi.mocked(request).mock.calls[0][0]).toBe("/platforms/A/queries");
  platform.currentId = "B";
  chat.forget("A");
  await nextTick();
  resolve({ text: "late A-private", ok: true });
  await flushPromises();
  expect(chat.conversations.A).toBeUndefined();
  expect(chat.conversation("B").entries).toEqual([]);
  view.unmount();
});
