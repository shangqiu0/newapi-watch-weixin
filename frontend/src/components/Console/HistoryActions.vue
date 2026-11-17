<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { usePlatformStore } from "@/store/modules/platform";
import { request, perform } from "@/api/console";
const props = defineProps<{
  kind: "logs" | "notifications" | "tasks" | "alerts" | "audits";
  ids?: string[];
  before?: number;
  label?: string;
}>();
const emit = defineEmits<{ changed: [] }>();
const platform = usePlatformStore(),
  busy = ref(false),
  visible = ref(false);
interface Preview {
  preview_id: string;
  count: number;
  protected: number;
  batch_size: number;
}
const plan = ref<Preview | null>(null),
  target = ref("");
const labels = {
  logs: "本地日志",
  notifications: "历史通知",
  tasks: "已结束任务",
  alerts: "历史告警",
  audits: "过期运行记录"
};
const name = computed(() => labels[props.kind]);
watch(
  () => platform.currentId,
  () => {
    visible.value = false;
    plan.value = null;
  }
);
async function preview(): Promise<void> {
  const id = platform.currentId;
  if (!id || busy.value) return;
  busy.value = true;
  try {
    await perform(async () => {
      const result = await request<Preview>(
        `/platforms/${id}/cleanup-previews`,
        "POST",
        {
          kind: props.kind,
          ...(props.ids?.length ? { ids: props.ids } : { before: props.before })
        }
      );
      if (platform.currentId === id) {
        plan.value = result;
        target.value = id;
        visible.value = true;
      }
    });
  } finally {
    busy.value = false;
  }
}
async function execute(): Promise<void> {
  const current = plan.value,
    id = target.value;
  if (!current || id !== platform.currentId || busy.value) return;
  try {
    await ElMessageBox.confirm(
      `将从「${platform.current?.name ?? id}」移除本批 ${current.batch_size} 条${name.value}。不会删除上游数据。`,
      "确认清理",
      {
        type: "warning",
        confirmButtonText: "确认清理",
        cancelButtonText: "取消"
      }
    );
  } catch {
    return;
  }
  if (id !== platform.currentId) return;
  busy.value = true;
  try {
    await perform(async () => {
      const result = await request<{ deleted: number; skipped: number }>(
        `/platforms/${id}/cleanups`,
        "POST",
        { preview_id: current.preview_id }
      );
      if (id === platform.currentId) {
        visible.value = false;
        emit("changed");
        if (result.skipped)
          await ElMessageBox.alert(
            `已清理 ${result.deleted} 条，${result.skipped} 条因状态变化或已不存在而跳过。`,
            "清理结果"
          );
      }
    }, "本批清理完成");
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <el-button
    :disabled="!platform.currentId || (ids !== undefined && !ids.length)"
    :loading="busy"
    @click="preview"
    >{{ label ?? `删除选中${name}` }}</el-button
  >
  <el-dialog
    v-model="visible"
    title="清理预览"
    width="min(520px, 94vw)"
    :close-on-click-modal="false"
    :show-close="!busy"
  >
    <template v-if="plan">
      <p class="cleanup-scope">{{ platform.current?.name }} · {{ name }}</p>
      <el-descriptions :column="1" border>
        <el-descriptions-item label="符合清理条件"
          >{{ plan.count }} 条</el-descriptions-item
        >
        <el-descriptions-item label="本批处理"
          >{{ plan.batch_size }} 条（每批最多 100 条）</el-descriptions-item
        >
        <el-descriptions-item label="受保护记录"
          >{{ plan.protected }} 条</el-descriptions-item
        >
      </el-descriptions>
      <p class="section-footnote">
        仅处理本系统的本地记录，不会删除上游日志。运行中任务、活动告警及投递中通知受保护。告警清理保留最小去重状态；更多记录可再次预览清理。
      </p>
    </template>
    <template #footer
      ><el-button :disabled="busy" @click="visible = false">取消</el-button
      ><el-button
        type="danger"
        :disabled="!plan?.batch_size"
        :loading="busy"
        @click="execute"
        >确认清理本批</el-button
      ></template
    >
  </el-dialog>
</template>
