<script setup lang="ts">
import { ref } from "vue";
import { ElMessage } from "element-plus";
import { request } from "@/api/console";
import type { Currency } from "@/api/types";
const model = defineModel<Currency>({ required: true });
const props = defineProps<{ previewPath?: string }>();
const reading = ref(false);
function change(
  field: "quota_per_unit" | "usd_exchange_rate",
  value: string
): void {
  model.value = { ...model.value, [field]: value, source: "manual" };
}
async function read(): Promise<void> {
  if (!props.previewPath || reading.value) return;
  const path = props.previewPath;
  reading.value = true;
  try {
    const candidate = await request<Currency>(path, "POST");
    if (props.previewPath !== path) return;
    model.value = candidate;
    ElMessage.success("已读取平台人民币显示参数，保存后生效");
  } catch (error) {
    ElMessage.error(
      error instanceof Error ? error.message : "读取失败，请手工核对"
    );
  } finally {
    reading.value = false;
  }
}
</script>
<template>
  <div class="currency-editor">
    <div class="form-grid">
      <el-form-item label="每系统单位对应 quota">
        <el-input
          :model-value="model.quota_per_unit"
          inputmode="decimal"
          aria-label="每系统单位对应 quota"
          @update:model-value="change('quota_per_unit', $event)"
        />
      </el-form-item>
      <el-form-item label="人民币显示倍率">
        <el-input
          :model-value="model.usd_exchange_rate"
          inputmode="decimal"
          aria-label="人民币显示倍率"
          @update:model-value="change('usd_exchange_rate', $event)"
        />
      </el-form-item>
    </div>
    <el-checkbox v-model="model.confirmed"
      >已核对与该平台页面的人民币显示口径一致</el-checkbox
    >
    <el-button v-if="previewPath" :loading="reading" @click="read"
      >读取平台参数</el-button
    >
    <p class="section-footnote">
      人民币 = 原始 quota ÷ 每系统单位 quota ×
      显示倍率。不是充值实付款或实时外汇价；不同上游各自配置。保存前不改变正在运行的阈值。
    </p>
  </div>
</template>
