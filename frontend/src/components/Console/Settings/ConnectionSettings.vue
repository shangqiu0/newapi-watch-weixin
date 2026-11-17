<script setup lang="ts">
import { reactive, watch, ref } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { useSettingsDraft } from "@/composables/useSettingsDraft";
import { perform, request } from "@/api/console";
const store = usePlatformStore(),
  dirty = useSettingsDraft(),
  saving = ref(false);
const form = reactive({
  name: "",
  newapi_user_id: 1,
  newapi_token: "",
  timezone: "Asia/Shanghai"
});
watch(
  form,
  () => {
    dirty.value = true;
  },
  { deep: true, flush: "sync" }
);
watch(
  () => store.current,
  current => {
    if (current && !dirty.value) {
      Object.assign(form, {
        name: current.name,
        newapi_user_id: current.newapi_user_id,
        newapi_token: "",
        timezone: current.timezone
      });
      dirty.value = false;
    }
  },
  { immediate: true }
);
async function save(): Promise<void> {
  const id = store.currentId,
    payload = { ...form };
  if (saving.value) return;
  saving.value = true;
  try {
    await perform(async () => {
      await request(`/platforms/${id}`, "PATCH", payload);
      if (store.currentId === id) {
        form.newapi_token = "";
        dirty.value = false;
        await store.load(id);
      }
    }, "接入设置已保存");
  } finally {
    saving.value = false;
  }
}
</script>
<template>
  <section class="surface settings-section">
    <div class="section-heading">
      <div>
        <h2>平台接入</h2>
        <p>主平台管理凭证与上游普通用户账户独立保存。</p>
      </div>
      <el-tag
        :type="store.current?.enabled ? 'success' : 'info'"
        effect="plain"
        >{{ store.current?.enabled ? "已启用" : "已停用" }}</el-tag
      >
    </div>
    <el-form label-position="top" @submit.prevent="save">
      <el-form-item label="平台名称"
        ><el-input v-model="form.name" maxlength="80" required
      /></el-form-item>
      <el-form-item label="平台地址"
        ><el-input :model-value="store.current?.newapi_url" disabled /><small
          >地址绑定本地历史，不支持直接更换；新站点请创建新的平台接入。</small
        ></el-form-item
      >
      <div class="form-grid">
        <el-form-item label="访问用户 ID"
          ><el-input-number
            v-model="form.newapi_user_id"
            :min="1" /></el-form-item
        ><el-form-item label="平台时区"
          ><el-select
            v-model="form.timezone"
            filterable
            allow-create
            default-first-option
            ><el-option
              label="北京时间 · Asia/Shanghai"
              value="Asia/Shanghai" /><el-option
              label="UTC"
              value="UTC" /><el-option
              label="美国东部 · America/New_York"
              value="America/New_York" /></el-select
        ></el-form-item>
      </div>
      <el-form-item label="管理访问令牌"
        ><el-input
          v-model="form.newapi_token"
          type="password"
          show-password
          autocomplete="new-password"
          placeholder="留空保留已保存的令牌"
        /><small
          >不是模型 API Key。令牌不会显示在列表或发给大模型。</small
        ></el-form-item
      >
      <div class="form-footer">
        <el-button
          type="primary"
          native-type="submit"
          :disabled="!dirty"
          :loading="saving"
          >保存接入设置</el-button
        >
      </div>
    </el-form>
  </section>
</template>
