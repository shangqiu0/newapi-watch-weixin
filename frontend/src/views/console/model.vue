<script setup lang="ts">
import { reactive, ref, watch } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { request, perform } from "@/api/console";
import type { Diagnostic } from "@/api/types";
import { timeCN } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import StatusDot from "@/components/Console/StatusDot.vue";
const store = usePlatformStore(),
  saving = ref(false),
  testing = ref(false),
  dirty = ref(false),
  diagnostic = ref<Diagnostic | null>(null);
const form = reactive({
  enabled: false,
  compatibility: "auto",
  base_url: "",
  model: "",
  api_key: ""
});
watch(
  () => store.overview,
  data => {
    if (!data) return;
    Object.assign(form, data.platform.llm, { api_key: "" });
    diagnostic.value = data.model_status;
    dirty.value = false;
  },
  { immediate: true }
);
async function save() {
  const id = store.currentId;
  saving.value = true;
  await perform(async () => {
    await request(`/platforms/${id}`, "PATCH", {
      llm: {
        enabled: form.enabled,
        compatibility: form.compatibility,
        base_url: form.base_url.trim(),
        model: form.model.trim(),
        api_key: form.api_key
      }
    });
    form.api_key = "";
    if (store.currentId === id) await store.load(id);
  }, "模型设置已保存，当前平台机器人已加载新配置");
  saving.value = false;
}
async function test() {
  testing.value = true;
  await perform(async () => {
    const id = store.currentId;
    const result = await request<Diagnostic>(
      `/platforms/${id}/model-checks`,
      "POST"
    );
    if (id === store.currentId) diagnostic.value = result;
  });
  testing.value = false;
}
</script>
<template>
  <ConsolePage
    title="模型服务"
    description="OpenAI Chat Completions 协议；先读取本平台真实数据，再组织回答"
    ><div class="settings-grid">
      <section class="surface">
        <div class="section-heading">
          <h2>接口配置</h2>
          <span class="context-label">仅当前平台生效</span>
        </div>
        <el-form
          label-position="top"
          @submit.prevent="save"
          @input="dirty = true"
          @change="dirty = true"
          ><el-form-item label="启用模型问答"
            ><el-switch
              v-model="form.enabled"
              :disabled="testing"
              @change="dirty = true" /></el-form-item
          ><el-form-item label="接口地址"
            ><el-input
              v-model="form.base_url"
              placeholder="https://api.deepseek.com"
              :disabled="testing"
            /><small
              >支持基础地址、/v1 基础地址或完整 /chat/completions 端点</small
            ></el-form-item
          >
          <div class="form-grid">
            <el-form-item label="模型名称"
              ><el-input
                v-model="form.model"
                :disabled="testing" /></el-form-item
            ><el-form-item label="兼容模式"
              ><el-select
                v-model="form.compatibility"
                :disabled="testing"
                @change="dirty = true"
                ><el-option value="auto" label="自动识别（推荐）" /><el-option
                  value="openai"
                  label="OpenAI Chat 兼容" /><el-option
                  value="deepseek"
                  label="DeepSeek 兼容" /></el-select
            ></el-form-item>
          </div>
          <el-form-item label="API Key"
            ><el-input
              v-model="form.api_key"
              type="password"
              show-password
              autocomplete="new-password"
              :disabled="testing"
              :placeholder="
                store.current?.llm.has_api_key
                  ? '密钥已保存，留空保持不变'
                  : '填写模型调用 Key，不是管理访问令牌'
              " /></el-form-item
          ><el-alert
            title="数据发送范围"
            description="模型只获得问题所需的绑定平台数据；管理令牌与机器人密钥不会进入提示词。监控告警不依赖模型"
            type="info"
            :closable="false"
          />
          <div class="form-footer">
            <el-button
              type="primary"
              native-type="submit"
              :loading="saving"
              :disabled="testing"
              >保存模型设置</el-button
            >
          </div></el-form
        >
      </section>
      <aside class="surface">
        <h2>连接诊断</h2>
        <div class="diagnostic-state">
          <StatusDot
            :text="
              testing
                ? '正在验证'
                : dirty
                  ? '更改尚未保存'
                  : diagnostic?.ok
                    ? '连接可用'
                    : diagnostic
                      ? '检查未通过'
                      : '尚未验证'
            "
            :tone="
              testing || dirty
                ? 'warn'
                : diagnostic?.ok
                  ? 'ok'
                  : diagnostic
                    ? 'danger'
                    : 'muted'
            "
          />
        </div>
        <el-alert
          v-if="!testing && !dirty && diagnostic"
          :type="diagnostic.ok ? 'success' : 'error'"
          :title="diagnostic.message"
          :closable="false"
        />
        <p v-else class="section-footnote">
          {{
            dirty
              ? "先保存配置后再测试；旧测试不代表新配置可用"
              : "验证鉴权与工具调用，使用少量模型 Token，不发送企微消息"
          }}
        </p>
        <el-descriptions :column="1" class="diagnostic-details"
          ><el-descriptions-item label="协议"
            >Chat Completions</el-descriptions-item
          ><el-descriptions-item label="HTTP">{{
            diagnostic?.http_status ?? "—"
          }}</el-descriptions-item
          ><el-descriptions-item label="最近检查">{{
            timeCN(diagnostic?.checked_at, store.current?.timezone)
          }}</el-descriptions-item></el-descriptions
        ><el-button
          :loading="testing"
          :disabled="dirty || saving || !store.current?.llm.has_api_key"
          @click="test"
          >测试已保存的连接</el-button
        >
        <div class="section-footnote">
          DeepSeek 官方端点自动关闭思考模式，以兼容强制工具调用；其他服务不发送
          DeepSeek 专有参数。保留你在服务方实际可用的模型 ID
        </div>
      </aside>
    </div></ConsolePage
  >
</template>
