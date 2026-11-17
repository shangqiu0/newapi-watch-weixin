<script setup lang="ts">
import { reactive, watch, ref } from "vue";
import { ElMessageBox } from "element-plus";
import { usePlatformStore } from "@/store/modules/platform";
import { useRemote } from "@/composables/useRemote";
import { useSettingsDraft } from "@/composables/useSettingsDraft";
import { request, perform } from "@/api/console";
import type { RetentionConfig } from "@/api/types";
import { timeCN, number } from "@/utils/format";
import HistoryActions from "@/components/Console/HistoryActions.vue";
interface Management {
  config: RetentionConfig;
  counts: Record<string, number>;
  last_run: {
    finished_at: number;
    deleted: Record<string, number> | number;
  } | null;
  next_check_at?: number | null;
}
const store = usePlatformStore(),
  dirty = useSettingsDraft(),
  saving = ref(false);
const remote = useRemote<Management>(() =>
  store.currentId ? `/platforms/${store.currentId}/data-management` : ""
);
const form = reactive<RetentionConfig>({
  enabled: true,
  logs_days: 30,
  history_days: 30,
  tasks_days: 1,
  audits_days: 30
});
watch(
  form,
  () => {
    dirty.value = true;
  },
  { deep: true, flush: "sync" }
);
watch(
  remote.data,
  data => {
    if (data && !dirty.value) {
      Object.assign(form, data.config);
      dirty.value = false;
    }
  },
  { immediate: true }
);
const fields = [
  {
    key: "logs_days",
    label: "本地请求日志",
    hint: "监控明细；不影响上游实时日志"
  },
  {
    key: "history_days",
    label: "历史告警与通知",
    hint: "保护活动告警、待投递和发送中记录"
  },
  {
    key: "tasks_days",
    label: "已结束查询任务",
    hint: "原策略为 1 天，可按需延长；活动任务有效期为 7 天"
  },
  {
    key: "audits_days",
    label: "运行与操作记录",
    hint: "只保留必要操作，不存储密钥或对话原文"
  }
] as const;
const cleanupKind = ref<
    "logs" | "notifications" | "tasks" | "alerts" | "audits"
  >("logs"),
  before = ref<number>(Date.now() - 30 * 86400000);
async function save(): Promise<void> {
  if (saving.value) return;
  const id = store.currentId;
  const payload = { ...form };
  const previous = remote.data.value?.config;
  const shortened =
    previous && fields.some(field => payload[field.key] < previous[field.key]);
  if (
    payload.enabled &&
    (shortened || previous?.enabled === false || !previous?.confirmed)
  ) {
    try {
      await ElMessageBox.confirm(
        "新的保留策略将在下一次后台检查起清理到期本地记录，删除后不可恢复。是否保存？",
        "确认数据保留策略",
        {
          type: "warning",
          confirmButtonText: "保存策略",
          cancelButtonText: "继续编辑"
        }
      );
    } catch {
      return;
    }
  }
  if (id !== store.currentId) return;
  saving.value = true;
  try {
    await perform(async () => {
      await request(`/platforms/${id}`, "PATCH", { retention: payload });
      if (id === store.currentId) {
        dirty.value = false;
        await remote.refresh();
        await store.load(id);
      }
    }, "保留策略已保存；后台按小时分批检查，不立即清空历史数据");
  } finally {
    saving.value = false;
  }
}
</script>
<template>
  <section class="surface settings-section">
    <div class="section-heading">
      <div>
        <h2>数据保留策略</h2>
        <p>只管理本服务的本地数据，不会删除上游日志或账户。</p>
      </div>
      <el-tag effect="plain">按平台独立配置</el-tag>
    </div>
    <el-alert
      v-if="remote.data.value && !remote.data.value.config.confirmed"
      type="info"
      :closable="false"
      title="首次保存后启用完整保留策略"
      description="为兼容旧版本，未确认前不会自动清理此前长期保留的失败通知和已恢复告警。保存后按当前天数处理。"
    />
    <el-alert
      v-if="remote.error.value"
      :title="remote.error.value"
      type="error"
      :closable="false"
    />
    <el-skeleton v-if="remote.loading.value && !remote.data.value" :rows="6" />
    <el-form v-else label-position="top" @submit.prevent="save">
      <div class="setting-row">
        <div>
          <strong>自动清理</strong>
          <p class="section-footnote">
            每小时检查到期记录，分批执行；关闭后仍保留必要的任务超时与传输收据维护。
          </p>
        </div>
        <el-switch v-model="form.enabled" aria-label="自动清理" />
      </div>
      <div v-for="field in fields" :key="field.key" class="setting-row">
        <div>
          <strong>{{ field.label }}</strong>
          <p class="section-footnote">{{ field.hint }}</p>
        </div>
        <el-form-item :label="`${field.label}保留天数`"
          ><el-input-number
            v-model="form[field.key]"
            :min="1"
            :max="365"
            controls-position="right"
            :disabled="!form.enabled"
        /></el-form-item>
      </div>
      <div class="form-footer">
        <el-button
          type="primary"
          native-type="submit"
          :loading="saving"
          :disabled="
            !remote.data.value ||
            (!dirty && !!remote.data.value.config.confirmed)
          "
          >保存保留策略</el-button
        >
      </div>
    </el-form>
  </section>
  <section class="surface">
    <div class="section-heading">
      <div>
        <h2>本地数据维护</h2>
        <p>先预览范围，再确认清理。每批最多 100 条，不能恢复。</p>
      </div>
    </div>
    <div class="data-inventory">
      <div
        v-for="(label, key) in {
          logs: '本地日志',
          notifications: '通知记录',
          tasks: '查询任务',
          audits: '命令记录'
        }"
        :key="key"
      >
        <span>{{ label }}</span
        ><strong>{{ number(remote.data.value?.counts[key]) }}</strong>
      </div>
    </div>
    <div class="table-toolbar">
      <el-select
        v-model="cleanupKind"
        aria-label="清理数据类型"
        style="width: 160px"
        ><el-option label="本地请求日志" value="logs" /><el-option
          label="历史通知"
          value="notifications" /><el-option
          label="已结束查询任务"
          value="tasks" /><el-option
          label="已恢复/历史告警"
          value="alerts" /><el-option label="过期命令记录" value="audits"
      /></el-select>
      <el-date-picker
        v-model="before"
        type="datetime"
        value-format="x"
        format="YYYY年M月D日 HH:mm:ss"
        placeholder="此时间之前"
        aria-label="清理截止时间"
      />
      <HistoryActions
        :kind="cleanupKind"
        :before="Number(before) / 1000"
        label="预览清理"
        @changed="remote.refresh"
      />
    </div>
    <p class="section-footnote">
      最近清理：{{
        timeCN(
          remote.data.value?.last_run?.finished_at,
          store.current?.timezone
        )
      }}。下次后台检查：{{
        timeCN(remote.data.value?.next_check_at, store.current?.timezone)
      }}。删除记录不代表 SQLite 文件立即缩小；不会自动执行全库压缩。
    </p>
  </section>
</template>
