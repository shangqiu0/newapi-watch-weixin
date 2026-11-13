import { beforeEach, describe, it, expect, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import type { Overview, Platform } from "../src/api/types";
vi.mock("@/api/console", () => ({ request: vi.fn() }));
import { request } from "../src/api/console";
import { usePlatformStore } from "../src/store/modules/platform";
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  let reject!: (e: Error) => void;
  return {
    promise: new Promise<T>((ok, no) => {
      resolve = ok;
      reject = no;
    }),
    resolve,
    reject
  };
};
const overview = (id: string) => ({ platform: { id, name: id } }) as Overview;
describe("平台上下文与防竞态", () => {
  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
    vi.mocked(request).mockReset();
  });
  it("旧平台慢请求不能覆盖新平台", async () => {
    const a = deferred<Overview>(),
      b = deferred<Overview>();
    vi.mocked(request).mockImplementation(path =>
      path.includes("/A/") ? a.promise : b.promise
    );
    const store = usePlatformStore();
    const first = store.select("A"),
      second = store.select("B");
    b.resolve(overview("B"));
    await second;
    a.resolve(overview("A"));
    await first;
    expect(store.currentId).toBe("B");
    expect(store.overview.platform.id).toBe("B");
    expect(store.loading).toBe(false);
  });
  it("请求错误显示错误而不是沿用上个平台数据", async () => {
    vi.mocked(request).mockResolvedValueOnce(overview("A"));
    const store = usePlatformStore();
    await store.select("A");
    vi.mocked(request).mockRejectedValueOnce(new Error("上游429"));
    await store.select("B");
    expect(store.overview).toBe(null);
    expect(store.error).toBe("上游429");
  });
  it("退出清空时未完成请求不能重新写回", async () => {
    const a = deferred<Overview>();
    vi.mocked(request).mockReturnValue(a.promise);
    const store = usePlatformStore();
    const task = store.select("A");
    store.clear();
    a.resolve(overview("A"));
    await task;
    expect(store.currentId).toBe("");
    expect(store.overview).toBe(null);
    expect(store.list).toEqual([]);
  });
});

it("刷新重新创建Store后恢复上次选择的平台，不回到列表第一项", async () => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(request).mockImplementation(path =>
    Promise.resolve(
      path === "/platforms"
        ? [{ id: "A" }, { id: "B" }]
        : overview(path.includes("/B/") ? "B" : "A")
    )
  );
  await usePlatformStore().load("B");
  setActivePinia(createPinia());
  const reloaded = usePlatformStore();
  await reloaded.load();
  expect(reloaded.currentId).toBe("B");
  expect(reloaded.overview.platform.id).toBe("B");
  localStorage.clear();
});
it("已删除的保存平台不会继续访问，回退到当前授权列表", async () => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(request).mockResolvedValue(overview("B"));
  await usePlatformStore().select("B");
  setActivePinia(createPinia());
  vi.mocked(request)
    .mockClear()
    .mockImplementation(path =>
      Promise.resolve(path === "/platforms" ? [{ id: "A" }] : overview("A"))
    );
  await usePlatformStore().load();
  expect(usePlatformStore().currentId).toBe("A");
  expect(
    vi.mocked(request).mock.calls.some(call => call[0].includes("/B/"))
  ).toBe(false);
  localStorage.clear();
});
