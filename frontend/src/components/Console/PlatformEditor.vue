<script setup lang="ts">
import { ref, reactive, watch, shallowRef } from "vue";
import { request, perform } from "@/api/console";
import { usePlatformStore } from "@/store/modules/platform";
import type { Platform } from "@/api/types";
const visible = defineModel<boolean>();
const props = defineProps<{ platform?: Platform }>();
const store = usePlatformStore();
const busy = ref(false);
const target = shallowRef<Platform | null>(null);
const values = reactive({
  name: "",
  newapi_url: "",
  newapi_token: "",
  newapi_user_id: 1,
  timezone: "Asia/Shanghai",
  enabled: false
});
watch(
  visible,
  open => {
    if (open) {
      target.value = props.platform ? { ...props.platform } : null;
      Object.assign(
        values,
        {
          name: "",
          newapi_url: "",
          newapi_token: "",
          newapi_user_id: 1,
          timezone: "Asia/Shanghai",
          enabled: false
        },
        target.value
          ? {
              name: target.value.name,
              newapi_url: target.value.newapi_url,
              newapi_user_id: target.value.newapi_user_id,
              timezone: target.value.timezone,
              enabled: target.value.enabled,
              newapi_token: ""
            }
          : {}
      );
    }
  },
  { immediate: true }
);
async function save(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    await perform(async () => {
      const editing = target.value;
      const payload = { ...values };
      if (editing) delete payload.newapi_url;
      const saved = await request<Platform>(
        editing ? `/platforms/${editing.id}` : "/platforms",
        editing ? "PATCH" : "POST",
        payload
      );
      visible.value = false;
      await store.load(editing ? undefined : saved.id);
    }, "平台已保存");
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <el-dialog
    v-model="visible"
    :title="target ? '编辑监控平台' : '新增监控平台'"
    width="min(580px, 96vw)"
    :close-on-click-modal="false"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    destroy-on-close
    ><el-form label-position="top" @submit.prevent="save"
      ><el-form-item label="平台名称"
        ><el-input
          v-model="values.name"
          aria-label="平台名称"
          maxlength="80"
          required /></el-form-item
      ><el-form-item label="NewAPI 地址"
        ><el-input
          v-model="values.newapi_url"
          aria-label="NewAPI 地址"
          :disabled="!!target"
          placeholder="https://your-platform.example"
          required /></el-form-item
      ><el-form-item label="管理访问令牌"
        ><el-input
          v-model="values.newapi_token"
          type="password"
          show-password
          :placeholder="
            target ? '已保存，留空保留原令牌' : '管理访问令牌，不是模型转发 Key'
          "
          autocomplete="new-password"
      /></el-form-item>
      <div class="form-grid">
        <el-form-item label="管理用户 ID"
          ><el-input-number
            v-model="values.newapi_user_id"
            :min="1" /></el-form-item
        ><el-form-item label="平台时区"
          ><el-input v-model="values.timezone"
        /></el-form-item>
      </div>
      <el-form-item label="启用监控"
        ><el-switch v-model="values.enabled" /></el-form-item
      ><el-alert
        title="平台地址创建后不可更换；检查频率与触发条件请到「监控策略」配置，不重启 NewAPI"
        type="info"
        :closable="false"
      />
      <div class="form-footer">
        <el-button :disabled="busy" @click="visible = false">取消</el-button
        ><el-button type="primary" native-type="submit" :loading="busy"
          >保存平台</el-button
        >
      </div></el-form
    ></el-dialog
  >
</template>
