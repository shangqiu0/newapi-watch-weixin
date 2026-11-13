import { describe, it, expect, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { effectScope } from "vue";
import { flushPromises } from "@vue/test-utils";
vi.mock("@/api/console", () => ({
  request: vi.fn(),
  perform: async (task: () => Promise<unknown>) => {
    try {
      await task();
    } catch {}
  }
}));
vi.mock("element-plus", () => ({
  ElMessage: { success: vi.fn(), error: vi.fn() }
}));
import { request } from "@/api/console";
import { useMonitoring } from "../src/composables/useMonitoring";
import { usePlatformStore } from "../src/store/modules/platform";

describe("监控表单的服务端错误", () => {
  it("保存失败保留编辑内容，并返回失败而不是关闭编辑器", async () => {
    setActivePinia(createPinia());
    usePlatformStore().currentId = "platform-A";
    vi.mocked(request).mockImplementation((_path, method) =>
      method === "PUT"
        ? Promise.reject(new Error("阈值无效"))
        : Promise.resolve({
            config: { balance_poll_seconds: 300, balances: [] },
            samples: [],
            errors: {}
          })
    );
    const scope = effectScope();
    const state = scope.run(() => useMonitoring());
    await flushPromises();
    state.config.value.balance_poll_seconds = 99;
    try {
      const result = await state.save();
      expect(result).toBe(false);
      expect(state.config.value.balance_poll_seconds).toBe(99);
      expect(state.saving.value).toBe(false);
    } finally {
      scope.stop();
    }
  });
});
