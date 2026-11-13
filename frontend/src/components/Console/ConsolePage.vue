<script setup lang="ts">
import { usePlatformStore } from "@/store/modules/platform";
import { perform } from "@/api/console";
import { timeCN } from "@/utils/format";
const props = withDefaults(
  defineProps<{
    title: string;
    description?: string;
    platformOptional?: boolean;
  }>(),
  { platformOptional: false }
);
const emit = defineEmits<{ refresh: [] }>();
async function refresh() {
  await perform(async () => {
    if (!props.platformOptional) await platform.refresh();
    emit("refresh");
  });
}
const platform = usePlatformStore();
</script>
<template>
  <div class="console-page">
    <div class="page-title-row">
      <div>
        <div class="context-label">
          {{
            props.platformOptional
              ? "全部监控平台"
              : (platform.current?.name ?? "工作空间")
          }}<span v-if="platform.current && !props.platformOptional">
            · 独立监控空间</span
          >
        </div>
        <h1>{{ props.title }}</h1>
        <p v-if="description">{{ description }}</p>
      </div>
      <div class="page-actions">
        <slot name="actions" /><el-button
          :loading="platform.loading"
          :disabled="!platform.currentId && !props.platformOptional"
          @click="refresh"
          >刷新状态</el-button
        >
      </div>
    </div>
    <el-alert
      v-if="platform.error"
      type="error"
      :title="platform.error"
      :closable="false"
      show-icon
    /><el-skeleton
      v-if="platform.loading && !platform.overview && !props.platformOptional"
      :rows="9"
      animated
    /><el-empty
      v-else-if="!platform.currentId && !props.platformOptional"
      description="还没有监控平台，请点击顶栏新增平台"
    /><slot v-else />
    <footer class="console-footer">
      <span>策略检查 · 不修改 NewAPI</span
      ><span
        >状态更新
        {{ timeCN(platform.updated, platform.current?.timezone) }}</span
      >
    </footer>
  </div>
</template>
