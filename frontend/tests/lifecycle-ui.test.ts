import { routeLocationKey } from "vue-router";
import { beforeEach, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus from "element-plus";
import Table from "@pureadmin/table";
vi.mock("@/api/console", () => ({
  request: vi.fn(),
  perform: async (fn: () => Promise<unknown>) => fn()
}));
import { request } from "@/api/console";
import { usePlatformStore } from "../src/store/modules/platform";
import Upstreams from "../src/views/console/upstreams.vue";
import Balances from "../src/views/console/balances.vue";
import DataManagement from "../src/components/Console/Settings/DataManagement.vue";
beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  usePlatformStore().currentId = "A";
});
it("上游账户分列显示今日和累计消费，未读取不伪造为零", async () => {
  vi.mocked(request).mockResolvedValue({
    items: [
      {
        id: "a",
        name: "供应商",
        base_url: "https://example.invalid",
        user_id: 7,
        currency: {},
        enabled: true,
        monitor_enabled: true,
        threshold_cny: "1234.5",
        threshold_display: "¥1,234.50",
        sample: {
          quota_display: "¥70.00",
          used_quota_display: "¥99.00",
          queried_at: 100
        },
        usage: {
          status: "ok",
          quota_display: "¥12.50",
          start_at: 10,
          end_at: 20,
          timezone: "Asia/Shanghai",
          stale: true
        }
      }
    ],
    total: 1
  });
  const view = mount(Upstreams, { global: { plugins: [ElementPlus, Table] } });
  await flushPromises();
  const headers = view.findAll("th").map(header => header.text());
  expect(headers[headers.indexOf("今日已用（¥）") + 1]).toBe("提醒阈值（¥）");
  expect(view.text()).toContain("¥1,234.50");
  expect(view.text()).toContain("¥12.50");
  expect(view.text()).toContain("累计已用");
  expect(view.text()).toContain("截至");
  expect(view.text()).not.toContain("Asia/Shanghai");
  expect(view.text()).not.toContain("零点至查询时");
  expect(view.text()).not.toContain("缓存已过期");
  view.unmount();
});
it("数据管理明确只清理本地数据，预览不会触发删除", async () => {
  vi.mocked(request).mockResolvedValue({
    config: {
      enabled: true,
      logs_days: 30,
      history_days: 30,
      tasks_days: 1,
      audits_days: 30
    },
    counts: { logs: 10, notifications: 2, tasks: 3, audits: 4 },
    last_run: null
  });
  const view = mount(DataManagement, { global: { plugins: [ElementPlus] } });
  await flushPromises();
  expect(view.text()).toContain("不会删除上游");
  expect(view.text()).toContain("保留天数");
  expect(
    vi.mocked(request).mock.calls.some(([, method]) => method === "DELETE")
  ).toBe(false);
  view.unmount();
});

it("缩短保留期需确认，取消后不提交配置", async () => {
  const { ElInputNumber, ElMessageBox } = await import("element-plus");
  const confirmation = vi
    .spyOn(ElMessageBox, "confirm")
    .mockRejectedValueOnce("cancel");
  vi.mocked(request).mockImplementation(async path =>
    path === "/platforms"
      ? []
      : {
          config: {
            enabled: true,
            logs_days: 30,
            history_days: 30,
            tasks_days: 1,
            audits_days: 30
          },
          counts: {},
          last_run: null
        }
  );
  const view = mount(DataManagement, { global: { plugins: [ElementPlus] } });
  await flushPromises();
  view.findAllComponents(ElInputNumber)[0].vm.$emit("update:modelValue", 7);
  await flushPromises();
  await view.find("form").trigger("submit");
  await flushPromises();
  expect(confirmation).toHaveBeenCalled();
  expect(
    vi.mocked(request).mock.calls.some(([, method]) => method === "PATCH")
  ).toBe(false);
  view.unmount();
  confirmation.mockRestore();
});

it("上游列表支持原生列设置，隐藏列不改动后端账户", async () => {
  const { ElCheckboxGroup } = await import("element-plus");
  vi.mocked(request).mockResolvedValue({ items: [], total: 0 });
  const view = mount(Upstreams, { global: { plugins: [ElementPlus, Table] } });
  await flushPromises();
  await view
    .findAll("button")
    .find(button => button.text() === "列设置")
    .trigger("click");
  await flushPromises();
  const settings = view.findComponent(ElCheckboxGroup);
  expect(settings.exists()).toBe(true);
  const visible = settings.props("modelValue") as string[];
  settings.vm.$emit(
    "update:modelValue",
    visible.filter(value => !value.includes("今日已用"))
  );
  await flushPromises();
  expect(
    view.findAll("th").some(header => header.text().includes("今日已用"))
  ).toBe(false);
  expect(
    vi
      .mocked(request)
      .mock.calls.every(([, method]) => !method || method === "GET")
  ).toBe(true);
  view.unmount();
});

it("数据管理显示各类最近清理数量及后台续清理状态", async () => {
  vi.mocked(request).mockResolvedValue({
    config: {
      enabled: true,
      confirmed: true,
      logs_days: 7,
      history_days: 30,
      tasks_days: 1,
      audits_days: 30
    },
    counts: {},
    next_check_at: 2000000000,
    last_run: {
      finished_at: 1999990000,
      automatic: true,
      enabled: true,
      deleted: {
        logs: 9,
        notifications: 2,
        tasks: 3,
        audits: 4,
        operations: 500
      },
      backlog: true
    }
  });
  const view = mount(DataManagement, { global: { plugins: [ElementPlus] } });
  await flushPromises();
  expect(view.text()).toContain("本地日志 9 条");
  expect(view.text()).toContain("操作记录 500 条");
  expect(view.text()).toContain("积压记录将在一分钟后继续清理");
  view.unmount();
});

it("用户余额监控将当前余额放在提醒阈值之前", async () => {
  vi.mocked(request).mockResolvedValue({
    config: { balances: [] },
    samples: []
  });
  const view = mount(Balances, {
    global: {
      plugins: [ElementPlus, Table],
      provide: { [routeLocationKey]: { query: {} } }
    }
  });
  await flushPromises();
  const headers = view.findAll("th").map(header => header.text());
  expect(headers.slice(0, 3)).toEqual(["用户", "当前余额", "提醒阈值（¥）"]);
  view.unmount();
});

it.each([false, true])(
  "阈值回填只显示两位小数并保留未编辑的 quota（修改金额：%s）",
  async edited => {
    const platform = usePlatformStore();
    platform.list = [
      {
        id: "A",
        name: "中转平台",
        currency: {
          confirmed: true,
          quota_per_unit: "500000",
          usd_exchange_rate: "7.3",
          source: "status"
        }
      }
    ] as typeof platform.list;
    vi.mocked(request).mockResolvedValue({
      config: {
        balance_poll_seconds: 300,
        balances: [
          {
            user_id: 29,
            name: "示例用户",
            threshold: 684931508,
            recovery_threshold: 9007199254740992,
            recovery_threshold_quota: "9007199254740993",
            threshold_cny: "10000.000022000000",
            recovery_threshold_cny: "10220.000014600000",
            enabled: true,
            confirmations: 1,
            cooldown_seconds: 300,
            binding_ids: [],
            notify_recovery: true,
            mention_all: false
          }
        ]
      },
      samples: [],
      errors: {}
    });
    const view = mount(Balances, {
      global: {
        plugins: [ElementPlus, Table],
        provide: { [routeLocationKey]: { query: {} } }
      }
    });
    await flushPromises();
    await view
      .findAll("button")
      .find(b => b.text() === "编辑")
      .trigger("click");
    await flushPromises();
    const threshold = view.get<HTMLInputElement>(
      'input[aria-label="余额阈值人民币"]'
    );
    expect(threshold.element.value).toBe("10000.00");
    expect(
      view.get<HTMLInputElement>('input[aria-label="恢复阈值人民币"]').element
        .value
    ).toBe("10220.00");
    if (edited) await threshold.setValue("¥10,001.50");
    await view.find("form").trigger("submit");
    await flushPromises();
    const payload = vi
      .mocked(request)
      .mock.calls.find(([, method]) => method === "PUT")?.[2];
    expect(payload).toMatchObject({
      balances: [
        { threshold: 684931508, recovery_threshold: "9007199254740993" }
      ]
    });
    if (edited)
      expect(payload).toHaveProperty("balance_amounts", {
        "29": { threshold_cny: "10001.50" }
      });
    else expect(payload).not.toHaveProperty("balance_amounts");
    view.unmount();
  }
);
