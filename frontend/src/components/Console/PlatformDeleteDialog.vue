<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import type { Platform } from "@/api/types";
import { usePlatformStore } from "@/store/modules/platform";
import { useChatStore } from "@/store/modules/chat";
const visible = defineModel<boolean>();
const props = defineProps<{ platform: Platform | null }>();
const store = usePlatformStore(),
  chat = useChatStore();
const name = ref(""),
  busy = ref(false),
  error = ref("");
const canDelete = computed(
  () => !!props.platform && name.value === props.platform.name && !busy.value
);
watch(visible, open => {
  if (open) {
    name.value = "";
    error.value = "";
  }
});
async function remove(): Promise<void> {
  if (!canDelete.value || !props.platform) return;
  const target = props.platform;
  busy.value = true;
  error.value = "";
  try {
    await store.remove(target.id, target.revision);
    chat.forget(target.id);
    visible.value = false;
    ElMessage.success("平台已移除，监控与机器人连接已停止；本地历史已保留");
  } catch (failure) {
    error.value =
      failure instanceof Error ? failure.message : "删除失败，请稍后重试";
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <el-dialog
    v-model="visible"
    title="删除监控平台"
    width="min(560px, 96vw)"
    :close-on-click-modal="false"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    destroy-on-close
  >
    <template v-if="platform"
      ><p class="delete-target">
        确认移除 <strong>{{ platform.name }}</strong
        >？
      </p>
      <p class="section-footnote">{{ platform.newapi_url }}</p>
      <el-alert
        type="warning"
        title="将停止此平台的监控任务与企业微信机器人连接"
        description="平台会从管理列表移除，本地历史记录与加密配置保留；不会修改或删除上游 NewAPI，也不会删除企业微信中的机器人"
        :closable="false"
        show-icon
      />
      <el-alert
        v-if="error"
        type="error"
        :title="error"
        :closable="false"
        show-icon
      />
      <el-form label-position="top" @submit.prevent="remove"
        ><el-form-item :label="`请输入平台名称「${platform.name}」确认`"
          ><el-input
            v-model="name"
            aria-label="输入平台名称确认删除"
            :disabled="busy"
            autocomplete="off"
        /></el-form-item>
        <div class="form-footer">
          <el-button :disabled="busy" @click="visible = false">取消</el-button
          ><el-button
            type="danger"
            native-type="button"
            :disabled="!canDelete"
            :loading="busy"
            @click="remove"
            >确认删除</el-button
          >
        </div></el-form
      >
    </template>
  </el-dialog>
</template>
<style scoped>
.delete-target {
  margin-bottom: 8px;
  font-size: 14px;
  color: var(--text);
}

.delete-target strong {
  font-weight: 500;
}

.el-alert {
  margin: 16px 0;
}
</style>
