import { describe, it, expect } from "vitest";
import { number, timeCN, shortDate, safeRedirect } from "../src/utils/format";
describe("统一读数与中文时间", () => {
  it("千分位、零值和空值显示一致", () => {
    expect(number(12345)).toBe("12,345");
    expect(number(0)).toBe("0");
    expect(number(null)).toBe("0");
  });
  it("按平台时区显示中文时间", () => {
    expect(timeCN("2026-11-11T08:30:05+08:00", "Asia/Shanghai")).toBe(
      "2026年11月11日 08:30:05"
    );
    expect(timeCN("invalid")).toBe("时间未知");
    expect(timeCN(null)).toBe("尚无记录");
  });
  it("短时间不暴露ISO", () => {
    expect(shortDate("2026-11-11T00:00:00+08:00")).toBe("11月11日");
  });
  it("登录重定向拒绝外站与协议相对地址", () => {
    for (const x of ["https://evil.invalid", "//evil.invalid", "/login", null])
      expect(safeRedirect(x)).toBe("/console/overview");
    expect(safeRedirect("/console/channels?page=2")).toBe(
      "/console/channels?page=2"
    );
  });
});

import { alertState } from "../src/utils/format";
it("旧采集告警标为历史记录，请求错误不暗示恢复状态", () => {
  expect(alertState({ state: "firing", key: "service:logs" }).text).toBe(
    "旧策略记录"
  );
  expect(alertState({ state: "firing", key: "request_error:old" }).text).toBe(
    "错误事件"
  );
  expect(
    alertState({ state: "firing", key: "policy:channel:1:status" }).text
  ).toBe("未恢复");
});
