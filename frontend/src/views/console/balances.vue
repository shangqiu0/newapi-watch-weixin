<script setup lang="ts">
import { ref, reactive, watch } from "vue";
import { useRoute } from "vue-router";
import { ElMessage } from "element-plus";
import { useMonitoring } from "@/composables/useMonitoring";
import { quotaText, timeCN } from "@/utils/format";
import type { BalanceRule } from "@/api/types";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import PolicyTargets from "@/components/Console/PolicyTargets.vue";
import StatusDot from "@/components/Console/StatusDot.vue";
const state = useMonitoring(),
  config = state.config,
  route = useRoute(),
  dialog = ref(false),
  editing = ref<number | null>(null);
const form = reactive<BalanceRule>({
  binding_ids: [],
  confirmations: 1,
  cooldown_seconds: 300,
  poll_seconds: null,
  recovery_threshold: null,
  user_id: 1,
  name: "",
  threshold: 100000,
  enabled: false,
  notify_recovery: true,
  mention_all: false
});
watch(
  () => state.platform.currentId,
  () => {
    dialog.value = false;
    editing.value = null;
  }
);
const amountThreshold = ref("0"),
  amountRecovery = ref("");
let initialThreshold = "",
  initialRecovery = "";
function edit(rule?: BalanceRule) {
  amountThreshold.value = rule?.threshold_cny ?? "0";
  amountRecovery.value = rule?.recovery_threshold_cny ?? "";
  initialThreshold = amountThreshold.value;
  initialRecovery = amountRecovery.value;
  editing.value = rule?.user_id ?? null;
  Object.assign(
    form,
    rule ?? {
      binding_ids: [],
      confirmations: 1,
      cooldown_seconds: 300,
      poll_seconds: null,
      recovery_threshold: null,
      user_id: Number(route.query.user) || 1,
      name: String(route.query.name ?? ""),
      threshold: 100000,
      enabled: false,
      notify_recovery: true,
      mention_all: false
    }
  );
  dialog.value = true;
}
async function save() {
  if (!config.value) return;
  const i = config.value.balances.findIndex(r => r.user_id === form.user_id);
  if (editing.value === null && i >= 0) {
    ElMessage.error("该用户已有余额监控，请使用编辑操作");
    return;
  }
  const changed =
    editing.value === null ||
    amountThreshold.value !== initialThreshold ||
    amountRecovery.value !== initialRecovery;
  if (changed && !state.platform.current?.currency?.confirmed) {
    ElMessage.error("请先到金额显示页面确认当前平台人民币换算");
    return;
  }
  if (i >= 0) config.value.balances[i] = { ...form };
  else config.value.balances.push({ ...form });
  const edits = changed
    ? {
        [String(form.user_id)]: {
          threshold_cny: amountThreshold.value,
          recovery_threshold_cny: amountRecovery.value || null
        }
      }
    : undefined;
  if (await state.save(edits)) dialog.value = false;
}
const sample = (id: number) => state.data.value?.samples.find(s => s.id === id);
const columns = [
  { label: "用户", slot: "user", minWidth: 180 },
  {
    label: "提醒阈值（¥）",
    align: "right",
    minWidth: 160,
    formatter: (r: BalanceRule) => quotaText(r.threshold, r.threshold_display)
  },
  { label: "当前余额", slot: "balance", align: "right", minWidth: 160 },
  { label: "监控状态", slot: "status", minWidth: 130 },
  {
    label: "最近成功检查",
    minWidth: 200,
    formatter: (r: BalanceRule) =>
      timeCN(sample(r.user_id)?.updated_at, state.platform.current?.timezone)
  },
  { label: "操作", slot: "action", fixed: "right", width: 160 }
];
watch(
  () => state.data.value,
  data => {
    if (
      data &&
      route.query.user &&
      !data.config.balances.some(r => r.user_id === Number(route.query.user))
    )
      edit();
  },
  { once: true }
);
</script>
<template>
  <ConsolePage
    title="余额监控"
    description="监控本平台指定用户账户，每个用户独立阈值与启停，不创建网站用户"
    @refresh="state.refresh"
    ><template #actions
      ><el-button type="primary" :disabled="!config" @click="edit()"
        >新增用户监控</el-button
      ></template
    ><el-alert
      v-if="state.error.value"
      :title="state.error.value"
      type="error"
      :closable="false"
    />
    <section v-if="config" class="surface">
      <div class="table-toolbar">
        <label>余额查询间隔（秒）</label
        ><el-input-number
          v-model="config.balance_poll_seconds"
          :min="30"
          :max="86400"
        /><el-button :loading="state.saving.value" @click="state.save()"
          >保存间隔</el-button
        ><span class="toolbar-meta"
          >{{ config.balances.length }} 个监控账户</span
        >
      </div>
      <pure-table :columns="columns" :data="config.balances" row-key="user_id"
        ><template #user="{ row }"
          >{{ row.name || "用户"
          }}<span class="cell-id">#{{ row.user_id }}</span></template
        ><template #balance="{ row }"
          ><span class="mono">{{
            sample(row.user_id)
              ? quotaText(
                  sample(row.user_id).quota,
                  sample(row.user_id).quota_display
                )
              : "尚未检查"
          }}</span
          ><el-tooltip
            v-if="state.data.value?.errors[String(row.user_id)]"
            :content="state.data.value.errors[String(row.user_id)]"
            ><span class="error-text">查询失败</span></el-tooltip
          ></template
        ><template #status="{ row }"
          ><StatusDot
            :text="
              !row.enabled
                ? '已停用'
                : state.data.value?.errors[String(row.user_id)]
                  ? '检查失败 · 未知'
                  : sample(row.user_id)?.is_low
                    ? '低于阈值'
                    : sample(row.user_id)
                      ? '检查成功'
                      : '等待首次检查'
            "
            :tone="
              !row.enabled
                ? 'muted'
                : state.data.value?.errors[String(row.user_id)] ||
                    sample(row.user_id)?.is_low
                  ? 'warn'
                  : sample(row.user_id)
                    ? 'ok'
                    : 'muted'
            " /></template
        ><template #action="{ row }"
          ><el-button link type="primary" @click="edit(row)">编辑</el-button
          ><el-button
            link
            @click="
              row.enabled = !row.enabled;
              state.save();
            "
            >{{ row.enabled ? "停用" : "启用" }}</el-button
          ><el-popconfirm
            title="删除此用户的余额监控？"
            @confirm="
              config.balances = config.balances.filter(
                r => r.user_id !== row.user_id
              );
              state.save();
            "
            ><template #reference
              ><el-button link type="danger">删除</el-button></template
            ></el-popconfirm
          ></template
        ></pure-table
      >
      <p class="section-footnote">
        阈值以人民币输入，后端按当前平台参数换算并继续保存整数
        quota。未确认金额参数时请先设置；查询失败不会被当成余额为零
      </p>
    </section>
    <el-dialog
      v-model="dialog"
      :title="editing ? '编辑用户余额监控' : '新增用户余额监控'"
      width="min(680px, 96vw)"
      destroy-on-close
      ><el-form label-position="top" @submit.prevent="save"
        ><el-form-item label="本平台用户 ID"
          ><el-input-number
            v-model="form.user_id"
            :min="1"
            :disabled="!!editing" /></el-form-item
        ><el-form-item label="监控名称"
          ><el-input v-model="form.name" maxlength="80" /></el-form-item
        ><el-form-item label="余额低于此阈值时提醒（¥）"
          ><el-input
            v-model="amountThreshold"
            aria-label="余额阈值人民币"
            inputmode="decimal"
            :disabled="!state.platform.current?.currency?.confirmed"
            placeholder="人民币金额，后端精确换算"
        /></el-form-item>
        <div class="form-grid">
          <el-form-item label="此用户检查间隔（秒，留空用平台默认）"
            ><el-input-number
              v-model="form.poll_seconds"
              :min="30"
              :max="86400" /></el-form-item
          ><el-form-item label="连续不足确认次数"
            ><el-input-number
              v-model="form.confirmations"
              :min="1"
              :max="20" /></el-form-item
          ><el-form-item label="恢复阈值（¥，可选）"
            ><el-input
              v-model="amountRecovery"
              aria-label="恢复阈值人民币"
              inputmode="decimal"
              :disabled="
                !state.platform.current?.currency?.confirmed
              " /></el-form-item
          ><el-form-item label="重复提醒冷却（秒）"
            ><el-input-number
              v-model="form.cooldown_seconds"
              :min="30"
              :max="86400"
          /></el-form-item>
        </div>
        <el-form-item label="通知 Webhook"
          ><PolicyTargets
            v-model="form.binding_ids"
            kind="balance" /></el-form-item
        ><el-form-item label="启用监控"
          ><el-switch v-model="form.enabled" /></el-form-item
        ><el-checkbox v-model="form.notify_recovery">余额恢复时通知</el-checkbox
        ><el-checkbox v-model="form.mention_all">@ 所有人</el-checkbox>
        <div class="form-footer">
          <el-button @click="dialog = false">取消</el-button
          ><el-button
            type="primary"
            native-type="submit"
            :loading="state.saving.value"
            >保存监控</el-button
          >
        </div></el-form
      ></el-dialog
    ></ConsolePage
  >
</template>
