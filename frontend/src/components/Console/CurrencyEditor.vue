<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import { request } from "@/api/console";
import type { Currency } from "@/api/types";
import { validateCurrencyDraft, yuanForEditing } from "@/utils/moneyInput";
const model = defineModel<Currency>({ required: true });
const props = defineProps<{ previewPath?: string; sourceLabel?: string }>();
const reading = ref(false);
const validation = computed(() => validateCurrencyDraft(model.value));
const example = computed(() =>
  validation.value ? "" : yuanForEditing(model.value.usd_exchange_rate.trim())
);
function change(
  field: "quota_per_unit" | "usd_exchange_rate",
  value: string
): void {
  if (model.value[field] === value) return;
  model.value = {
    ...model.value,
    [field]: value,
    confirmed: false,
    source: "manual"
  };
}
async function read(): Promise<void> {
  if (!props.previewPath || reading.value) return;
  const path = props.previewPath;
  const draft = model.value;
  reading.value = true;
  try {
    const candidate = await request<Currency>(path, "POST");
    if (props.previewPath !== path || model.value !== draft) return;
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
  <div class="currency-editor money-form-section">
    <div class="money-form-options">
      <el-tag :type="model.confirmed ? 'success' : 'info'" effect="plain">
        {{
          model.confirmed
            ? model.source === "status"
              ? "来自站点 · 已确认"
              : "已手动确认"
            : "未确认 · 不显示人民币"
        }}
      </el-tag>
      <el-button v-if="previewPath" :loading="reading" @click="read"
        >读取平台参数</el-button
      >
    </div>
    <p class="section-footnote">
      {{
        sourceLabel ?? "当前平台"
      }}独立换算。自动读取仅接受明确声明为人民币（CNY）的站点参数，读取只更新草稿。
    </p>
    <div class="form-grid">
      <el-form-item label="1 USD 对应的 quota">
        <el-input
          :model-value="model.quota_per_unit"
          inputmode="decimal"
          maxlength="32"
          aria-label="每系统单位对应 quota"
          placeholder="例如 500000"
          @update:model-value="change('quota_per_unit', $event)"
        />
      </el-form-item>
      <el-form-item label="1 USD 折合人民币">
        <el-input
          :model-value="model.usd_exchange_rate"
          inputmode="decimal"
          maxlength="32"
          aria-label="人民币显示倍率"
          placeholder="例如 7.3"
          @update:model-value="change('usd_exchange_rate', $event)"
          ><template #prepend>¥</template></el-input
        >
      </el-form-item>
    </div>
    <p v-if="validation" class="section-footnote" role="alert">
      {{ validation }}
    </p>
    <p v-else class="section-footnote">
      按当前填写值，{{ model.quota_per_unit }} quota = 1 USD ≈ ¥{{
        example
      }}。这是额度显示，不是充值实付款。
    </p>
    <el-checkbox
      :model-value="model.confirmed"
      :disabled="!!validation"
      @update:model-value="
        checked => (model = { ...model, confirmed: checked === true })
      "
      >已与该站点的人民币显示口径核对</el-checkbox
    >
  </div>
</template>
