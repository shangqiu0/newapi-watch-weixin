import { beforeEach, it, expect, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import type { Overview, Platform } from "../src/api/types";
vi.mock("@/api/console", () => ({ request: vi.fn() }));
import { request } from "@/api/console";
import { usePlatformStore } from "../src/store/modules/platform";
import { useChatStore } from "../src/store/modules/chat";
const platform = (id: string) => ({ id, name: id }) as Platform;
const overview = (id: string) => ({ platform: platform(id) }) as Overview;
beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(request).mockReset();
});
it("删除当前平台后选择有效平台，迟到的旧平台快照不能写回", async () => {
  const store = usePlatformStore();
  store.list = [platform("A"), platform("B")];
  let resolveOld: (value: Overview) => void;
  vi.mocked(request).mockImplementation(path =>
    path.includes("/A/overview")
      ? new Promise(resolve => {
          resolveOld = resolve;
        })
      : Promise.resolve(
          path.includes("/B/") ? overview("B") : { deleted: true }
        )
  );
  const old = store.select("A");
  await store.remove("A", 3);
  resolveOld(overview("A"));
  await old;
  expect(store.currentId).toBe("B");
  expect(store.overview.platform.id).toBe("B");
  expect(store.list.map(p => p.id)).toEqual(["B"]);
  expect(
    vi
      .mocked(request)
      .mock.calls.some(
        c => c[0] === "/platforms/A?revision=3" && c[1] === "DELETE"
      )
  ).toBe(true);
});
it("删除非当前平台不切换上下文，不清空其他平台快照", async () => {
  const store = usePlatformStore();
  store.list = [platform("A"), platform("B")];
  store.currentId = "B";
  store.overview = overview("B");
  vi.mocked(request).mockResolvedValue({ deleted: true });
  await store.remove("A");
  expect(store.currentId).toBe("B");
  expect(store.overview.platform.id).toBe("B");
  expect(vi.mocked(request)).toHaveBeenCalledTimes(1);
});
it("删除最后平台后清空当前选择；刷新前旧列表响应不能复活已删条目", async () => {
  const store = usePlatformStore();
  store.list = [platform("A")];
  store.currentId = "A";
  let resolveList: (value: Platform[]) => void;
  vi.mocked(request).mockImplementation(path =>
    path === "/platforms"
      ? new Promise(resolve => {
          resolveList = resolve;
        })
      : Promise.resolve({ deleted: true })
  );
  const loadingList = store.load();
  await store.remove("A");
  resolveList([platform("A")]);
  await loadingList;
  expect(store.currentId).toBe("");
  expect(store.list).toEqual([]);
  expect(store.overview).toBe(null);
});
it("删除请求失败不移除列表、不变更当前平台", async () => {
  const store = usePlatformStore();
  store.list = [platform("A")];
  store.currentId = "A";
  vi.mocked(request).mockRejectedValue(new Error("平台已更新，请刷新"));
  await expect(store.remove("A", 1)).rejects.toThrow("平台已更新");
  expect(store.list.map(p => p.id)).toEqual(["A"]);
  expect(store.currentId).toBe("A");
});
it("仅移除被删除平台的聊天，其余平台草稿与历史保留", () => {
  const chat = useChatStore();
  chat.append("A", { role: "user", text: "A-private" });
  chat.append("B", { role: "user", text: "B-private" });
  chat.conversation("B").draft = "unsent";
  chat.forget("A");
  expect(chat.conversations.A).toBeUndefined();
  expect(chat.conversations.B.entries[0].text).toBe("B-private");
  expect(chat.conversations.B.draft).toBe("unsent");
});
