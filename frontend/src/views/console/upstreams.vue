<script setup lang="ts">
import { usePageBounds } from "@/composables/usePageBounds";
import { ref, reactive, computed, watch } from "vue";
import { ElMessageBox, ElMessage } from "element-plus";
import { usePlatformStore } from "@/store/modules/platform";
import { usePagedRemote } from "@/composables/usePagedRemote";
import { request, perform } from "@/api/console";
import type { UpstreamAccount, Currency, Page } from "@/api/types";
import { timeCN } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import CurrencyEditor from "@/components/Console/CurrencyEditor.vue";
import YuanThresholdFields from "@/components/Console/YuanThresholdFields.vue";
import {
  readYuanThresholds,
  validateCurrencyDraft,
  yuanForEditing
} from "@/utils/moneyInput";
const platform = usePlatformStore(),
  page = ref(1),
  size = ref(20);
watch([() => platform.currentId, size], () => {
  page.value = 1;
  dialog.value = false;
});
const { data, loading, error, refresh, total } = usePagedRemote<
  Page<UpstreamAccount>
>(
  () =>
    platform.currentId
      ? `/platforms/${platform.currentId}/upstream-accounts?page=${page.value}&page_size=${size.value}`
      : "",
  () => platform.currentId
);
const dialog = ref(false),
  editing = ref<string | null>(null),
  busy = ref(false),
  querying = ref("");
function initial() {
  return {
    name: "",
    base_url: "",
    user_id: 1,
    access_token: "",
    enabled: false,
    usage_timezone: "",
    monitor_enabled: false,
    poll_seconds: 300,
    currency: {
      confirmed: false,
      quota_per_unit: "500000",
      usd_exchange_rate: "1",
      source: "manual"
    } as Currency,
    threshold_cny: "10",
    recovery_threshold_cny: "",
    cooldown_seconds: 1800,
    notify_recovery: true,
    query_binding_ids: [] as string[],
    notify_binding_ids: [] as string[]
  };
}
const form = reactive(initial());
const amountError = ref("");
const original = ref<UpstreamAccount | null>(null);
const sameSource = computed(
  () =>
    original.value !== null &&
    form.base_url.trim() === original.value.base_url &&
    form.user_id === original.value.user_id
);
const currencyPreviewPath = computed(() =>
  editing.value && sameSource.value
    ? `/platforms/${platform.currentId}/upstream-accounts/${editing.value}/currency-previews`
    : undefined
);
watch([() => form.base_url, () => form.user_id], () => {
  if (
    dialog.value &&
    original.value &&
    !sameSource.value &&
    form.currency.confirmed
  ) {
    form.currency = { ...form.currency, source: "manual", confirmed: false };
  }
});
const bindings = computed(() => platform.overview?.bindings ?? []);
const bots = computed(() => bindings.value.filter(b => b.kind === "bot"));
const targets = computed(() =>
  bindings.value.filter(
    b =>
      b.events.includes("balance") &&
      (b.kind === "webhook" || b.notification_chat_id)
  )
);
function edit(account?: UpstreamAccount): void {
  editing.value = account?.id ?? null;
  original.value = account ?? null;
  amountError.value = "";
  const values = initial();
  if (account)
    for (const key of Object.keys(values)) {
      if (key !== "access_token" && key in account)
        Object.assign(values, { [key]: account[key as keyof UpstreamAccount] });
    }
  Object.assign(
    form,
    JSON.parse(
      JSON.stringify({
        ...values,
        threshold_cny: yuanForEditing(
          account?.threshold_cny ?? values.threshold_cny
        ),
        recovery_threshold_cny: yuanForEditing(account?.recovery_threshold_cny),
        access_token: ""
      })
    )
  );
  dialog.value = true;
}
async function save(): Promise<void> {
  if (busy.value) return;
  amountError.value = "";
  const currencyError = validateCurrencyDraft(form.currency);
  if (currencyError) {
    ElMessage.error(currencyError);
    return;
  }
  const saved = original.value;
  let values: ReturnType<typeof readYuanThresholds>;
  try {
    values = readYuanThresholds(
      form.threshold_cny,
      form.recovery_threshold_cny
    );
  } catch (error) {
    amountError.value = (error as Error).message;
    return;
  }
  if (form.monitor_enabled && !form.currency.confirmed) {
    amountError.value = "开启余额监控前，请先确认该上游账户的人民币换算参数";
    return;
  }
  busy.value = true;
  const id = platform.currentId;
  try {
    await perform(async () => {
      const payload = {
        ...form,
        currency: {
          ...form.currency,
          quota_per_unit: form.currency.quota_per_unit.trim(),
          usd_exchange_rate: form.currency.usd_exchange_rate.trim()
        },
        threshold_cny:
          saved && values.low === yuanForEditing(saved.threshold_cny)
            ? saved.threshold_cny
            : values.low,
        recovery_threshold_cny:
          saved &&
          (values.recovery ?? "") ===
            yuanForEditing(saved.recovery_threshold_cny)
            ? saved.recovery_threshold_cny
            : values.recovery
      };
      await request(
        `/platforms/${id}/upstream-accounts${editing.value ? "/" + editing.value : ""}`,
        editing.value ? "PATCH" : "POST",
        payload
      );
      if (platform.currentId === id) {
        dialog.value = false;
        await refresh();
      }
    }, "上游账户已保存");
  } finally {
    busy.value = false;
  }
}
async function remove(account: UpstreamAccount): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除「${account.name}」的接入和余额监控？不会删除上游账户。`,
      "删除上游接入",
      { type: "warning" }
    );
  } catch {
    return;
  }
  const id = platform.currentId;
  await perform(async () => {
    await request(`/platforms/${id}/upstream-accounts/${account.id}`, "DELETE");
    if (platform.currentId === id) await refresh();
  }, "上游接入已删除");
}
async function query(account: UpstreamAccount): Promise<void> {
  if (querying.value) return;
  const id = platform.currentId;
  querying.value = account.id;
  try {
    const result = await request<{ quota_display: string }>(
      `/platforms/${id}/upstream-accounts/${account.id}/balance-queries`,
      "POST"
    );
    if (platform.currentId === id) {
      ElMessage.success(`${account.name}：${result.quota_display}`);
      await refresh();
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "查询失败");
  } finally {
    querying.value = "";
  }
}
usePageBounds(total, page, size);
const columns = [
  { label: "上游账户", slot: "account", minWidth: 190 },
  { label: "可用余额（¥）", slot: "balance", minWidth: 160, align: "right" },
  { label: "今日已用（¥）", slot: "usage", minWidth: 220, align: "right" },
  { label: "提醒阈值（¥）", slot: "threshold", minWidth: 145, align: "right" },
  { label: "接入 / 监控", slot: "status", minWidth: 120 },
  { label: "最近读取", slot: "time", minWidth: 200 },
  { label: "操作", slot: "action", fixed: "right", width: 200 }
];
const visibleLabels = ref(columns.map(column => column.label));
const displayedColumns = computed(() =>
  columns.filter(
    column =>
      column.slot === "account" ||
      column.slot === "action" ||
      visibleLabels.value.includes(column.label)
  )
);
</script>
<template>
  <ConsolePage
    title="上游账户"
    description="用普通用户访问令牌读取上游自己的余额；与本平台管理员查询、其他上游和其他机器人隔离"
    @refresh="refresh"
  >
    <template #actions
      ><el-button type="primary" :disabled="!platform.currentId" @click="edit()"
        >新增上游账户</el-button
      ></template
    >
    <section class="surface">
      <el-alert v-if="error" :title="error" type="error" :closable="false" />
      <div class="table-toolbar list-heading">
        <span class="toolbar-meta"
          >{{ total }} 个上游账户 · 今日消费独立统计</span
        >
        <el-popover placement="bottom-end" trigger="click" :width="220"
          ><template #reference><el-button>列设置</el-button></template>
          <div class="column-settings">
            <strong>显示列</strong
            ><el-checkbox-group v-model="visibleLabels"
              ><el-checkbox
                v-for="column in columns"
                :key="column.label"
                :value="column.label"
                :disabled="['account', 'action'].includes(column.slot)"
                >{{ column.label }}</el-checkbox
              ></el-checkbox-group
            ><el-button
              link
              type="primary"
              @click="visibleLabels = columns.map(column => column.label)"
              >恢复默认</el-button
            >
          </div>
        </el-popover>
      </div>
      <p class="mobile-table-hint">左右滑动查看余额、今日用量与操作</p>
      <pure-table
        :columns="displayedColumns"
        :data="data?.items ?? []"
        :loading="loading"
        row-key="id"
      >
        <template #account="{ row }"
          ><strong>{{ row.name }}</strong>
          <div class="muted">
            {{ row.base_url }} · 用户 #{{ row.user_id }}
          </div></template
        >
        <template #balance="{ row }"
          ><strong>{{ row.sample?.quota_display ?? "尚未读取" }}</strong>
          <div class="muted">
            累计已用 {{ row.sample?.used_quota_display ?? "未提供" }}
          </div>
          <div v-if="row.error" class="muted">
            读取失败，保留旧值：{{ row.error }}
          </div></template
        >
        <template #usage="{ row }">
          <strong class="amount-value">{{
            row.usage?.quota_display ??
            (row.usage?.status === "error" ? "读取失败" : "尚未读取")
          }}</strong>
          <div v-if="row.usage?.end_at" class="section-footnote">
            截至 {{ timeCN(row.usage.end_at, row.usage.timezone) }}
          </div>
          <el-tooltip v-if="row.usage?.error" :content="row.usage.error"
            ><el-tag type="warning" size="small" effect="plain">{{
              row.usage.quota_display ? "旧值 · 统计读取失败" : "统计暂不可用"
            }}</el-tag></el-tooltip
          >
        </template>
        <template #threshold="{ row }">
          {{
            row.monitor_enabled ? (row.threshold_display ?? "未提供") : "未启用"
          }}
        </template>
        <template #status="{ row }"
          ><el-tag :type="row.enabled ? 'success' : 'info'">{{
            row.enabled ? "已启用" : "已停用"
          }}</el-tag>
          <div class="muted">
            {{ row.monitor_enabled ? "余额监控开启" : "仅按需查询" }}
          </div></template
        >
        <template #time="{ row }">{{
          timeCN(row.sample?.queried_at, platform.current?.timezone)
        }}</template>
        <template #action="{ row }"
          ><el-button
            link
            type="primary"
            :disabled="!row.enabled || !!querying"
            :loading="querying === row.id"
            @click="query(row)"
            >刷新数据</el-button
          ><el-button link @click="edit(row)">编辑</el-button
          ><el-button link type="danger" @click="remove(row)"
            >删除</el-button
          ></template
        >
      </pure-table>
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="size"
        :page-sizes="[20, 50, 100]"
        :total="total"
        layout="total,sizes,prev,pager,next"
      />
      <p class="section-footnote">
        余额读取 /api/user/self，今日消费读取
        /api/log/self/stat；不支持时显示原因，不扫描全量日志。Webhook
        是单向通知，不能接收聊天查询。机器人可发送“上游余额”或“上游余额
        账户名称”。
      </p>
    </section>
    <el-drawer
      v-model="dialog"
      :title="editing ? '编辑上游账户' : '新增上游账户'"
      size="min(720px,96vw)"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <el-form label-position="top" @submit.prevent="save">
        <section class="money-form-section" aria-label="账户接入">
          <h3>账户接入</h3>
          <div class="form-grid">
            <el-form-item label="账户名称"
              ><el-input
                v-model="form.name"
                required
                maxlength="80" /></el-form-item
            ><el-form-item label="上游平台地址"
              ><el-input
                v-model="form.base_url"
                placeholder="https://upstream.example"
                required /></el-form-item
            ><el-form-item label="上游自身用户 ID"
              ><el-input-number v-model="form.user_id" :min="1" /></el-form-item
            ><el-form-item label="普通用户访问令牌（不是模型 Key）"
              ><el-input
                v-model="form.access_token"
                type="password"
                show-password
                autocomplete="new-password"
                :placeholder="
                  editing
                    ? '留空保留原令牌；更换地址或用户须重填'
                    : '仅需该上游的普通用户令牌'
                "
            /></el-form-item>
          </div>
          <el-form-item label="启用接入"
            ><el-switch v-model="form.enabled"
          /></el-form-item>
        </section>
        <section class="money-form-section" aria-label="金额与统计">
          <h3>金额与统计</h3>
          <el-form-item label="今日统计时区"
            ><el-select
              v-model="form.usage_timezone"
              filterable
              allow-create
              default-first-option
              ><el-option
                value=""
                :label="`继承平台时区 · ${platform.current?.timezone ?? 'Asia/Shanghai'}`" /><el-option
                value="Asia/Shanghai"
                label="北京时间 · Asia/Shanghai" /><el-option
                value="America/New_York"
                label="美国东部 · America/New_York" /><el-option
                value="UTC"
                label="UTC" /></el-select
          ></el-form-item>
          <CurrencyEditor
            v-model="form.currency"
            :preview-path="currencyPreviewPath"
            source-label="该上游站点"
          />
          <p v-if="!currencyPreviewPath" class="section-footnote">
            账户首次保存或地址有更改时，保存后再读取对应站点参数；也可以先手动核对。
          </p>
        </section>
        <section class="money-form-section" aria-label="余额监控与通知">
          <h3>余额监控与通知</h3>
          <el-form-item label="定时监控余额"
            ><el-switch v-model="form.monitor_enabled"
          /></el-form-item>
          <YuanThresholdFields
            v-model:low="form.threshold_cny"
            v-model:recovery="form.recovery_threshold_cny"
            :error="amountError"
            @edited="amountError = ''"
          />
          <p class="section-footnote">
            该账户以人民币阈值为准；更改换算参数后，比较用的 quota
            会按新口径计算。
          </p>
          <el-form-item label="允许查询的本平台机器人"
            ><el-select
              v-model="form.query_binding_ids"
              multiple
              placeholder="留空则不开放给机器人；网页仍可查询"
              ><el-option
                v-for="b in bots"
                :key="b.id"
                :value="b.id"
                :label="b.name" /></el-select
          ></el-form-item>
          <div class="form-grid">
            <el-form-item label="检查间隔（秒）"
              ><el-input-number
                v-model="form.poll_seconds"
                :min="30"
                :max="86400" /></el-form-item
            ><el-form-item label="重复提醒冷却（秒）"
              ><el-input-number
                v-model="form.cooldown_seconds"
                :min="30"
                :max="86400"
            /></el-form-item>
          </div>
          <el-form-item label="余额通知目标"
            ><el-select
              v-model="form.notify_binding_ids"
              multiple
              placeholder="选择本平台 Webhook 或已设置接收会话的机器人"
              ><el-option
                v-for="b in targets"
                :key="b.id"
                :value="b.id"
                :label="b.name" /></el-select
          ></el-form-item>
          <el-checkbox v-model="form.notify_recovery"
            >余额恢复时通知</el-checkbox
          >
          <p class="section-footnote">
            保存不发送测试通知。开启监控后按配置检查；参数不全、身份不匹配和接口拒绝不会被当成零余额。先保存账户后可读取该上游的金额参数。
          </p>
        </section>
        <div class="form-footer">
          <el-button @click="dialog = false">取消</el-button
          ><el-button type="primary" native-type="submit" :loading="busy"
            >保存上游账户</el-button
          >
        </div>
      </el-form>
    </el-drawer>
  </ConsolePage>
</template>
