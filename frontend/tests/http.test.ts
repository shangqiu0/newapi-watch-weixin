import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import axios, {
  type AxiosAdapter,
  type InternalAxiosRequestConfig
} from "axios";
let handler: AxiosAdapter;
beforeEach(() => {
  vi.resetModules();
  axios.defaults.adapter = config => handler(config);
});
afterEach(() => {
  axios.defaults.adapter = undefined;
});
describe("Cookie + CSRF API契约", () => {
  it("写操作携带CSRF，响应只解包data", async () => {
    let received: InternalAxiosRequestConfig;
    handler = async config => {
      received = config;
      return {
        data: { code: 0, data: { ok: true }, message: "success" },
        status: 200,
        statusText: "OK",
        headers: {},
        config
      };
    };
    const { request, setCsrf } = await import("../src/api/console");
    setCsrf("test-only");
    expect(await request("/platforms", "POST", { name: "demo" })).toEqual({
      ok: true
    });
    expect(received.headers["X-CSRF-Token"]).toBe("test-only");
    expect(received.withCredentials).toBe(true);
  });
  it("初始会话401不会广播重入登录事件", async () => {
    handler = async config => {
      throw new axios.AxiosError("unauthorized", "401", config, undefined, {
        data: { message: "请登录" },
        status: 401,
        statusText: "Unauthorized",
        headers: {},
        config
      });
    };
    let events = 0;
    const callback = () => events++;
    window.addEventListener("session-expired", callback);
    try {
      const { request } = await import("../src/api/console");
      await expect(request("/session")).rejects.toThrow("请登录");
      expect(events).toBe(0);
      await expect(request("/platforms")).rejects.toThrow("请登录");
      expect(events).toBe(1);
    } finally {
      window.removeEventListener("session-expired", callback);
    }
  });
  it("取消请求不转化成服务错误", async () => {
    handler = async () => {
      throw new axios.CanceledError("cancel");
    };
    const { request } = await import("../src/api/console");
    try {
      await request("/platforms");
      throw new Error("expected rejection");
    } catch (error) {
      expect(axios.isCancel(error)).toBe(true);
    }
  });
});
