import { describe, it, expect } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import MonitorChecks from "../src/components/Console/MonitorChecks.vue";
import type { MonitorCheck } from "../src/api/types";

const check = (patch: Partial<MonitorCheck> = {}): MonitorCheck => ({
  id: "channels",
  name: "渠道状态",
  kind: "channel",
  enabled: true,
  interval: 60,
  status: "unknown",
  last_at: 1794400000,
  last_ok_at: 1794399900,
  next_at: 1794400060,
  failures: 2,
  error: "NewAPI HTTP 429",
  detail: {},
  stale: false,
  ...patch
});
describe("检查状态真实呈现", () => {
  it("上游失败显示未知与原因，不伪装渠道已恢复", async () => {
    const view = mount(MonitorChecks, { props: { checks: [check()] } });
    await flushPromises();
    expect(view.text()).toContain("检查失败 · 状态未知");
    expect(view.text()).toContain("NewAPI HTTP 429");
    expect(view.text()).toContain("2026年");
    expect(view.text()).not.toContain("渠道正常");
  });
  it("停用策略不显示下一次检查且没有健康承诺", async () => {
    const view = mount(MonitorChecks, {
      props: { checks: [check({ enabled: false, status: "disabled" })] }
    });
    await flushPromises();
    expect(view.text()).toContain("已停用");
    expect(view.text()).toContain("不执行");
  });
  it("没有策略提供配置引导", async () => {
    const view = mount(MonitorChecks, { props: { checks: [] } });
    expect(view.text()).toContain("配置监控策略");
  });
});

it("报告分批处理中不是检查失败，显示已读取进度与续跑状态", async () => {
  const view = mount(MonitorChecks, {
    props: {
      checks: [
        check({
          id: "daily",
          kind: "daily",
          name: "日报调度",
          status: "pending",
          error: "",
          detail: {
            report_stage: "collecting",
            records_fetched: 500,
            total_records: 2000,
            note: "分批汇总，下轮续读"
          }
        })
      ]
    }
  });
  await flushPromises();
  expect(view.text()).toContain("报告汇总中");
  expect(view.text()).toContain("500 / 2,000");
  expect(view.text()).not.toContain("检查失败");
  view.unmount();
});
it("失败达到上限显示本期暂停，不假装已投递报告", async () => {
  const view = mount(MonitorChecks, {
    props: {
      checks: [
        check({
          id: "daily",
          kind: "daily",
          name: "日报调度",
          detail: {
            report_stage: "paused",
            records_fetched: 100,
            total_records: 2000
          },
          error: "NewAPI HTTP 503"
        })
      ]
    }
  });
  await flushPromises();
  expect(view.text()).toContain("本期已暂停");
  expect(view.text()).not.toContain("报告已入队");
  view.unmount();
});
