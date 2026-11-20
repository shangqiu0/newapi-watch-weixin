<script setup lang="ts">
import { normalizeYuan } from "@/utils/moneyInput";
const low = defineModel<string>("low", { required: true });
const recovery = defineModel<string>("recovery", { required: true });
defineProps<{ disabled?: boolean; error?: string }>();
defineEmits<{ edited: [] }>();
</script>

<template>
  <div class="form-grid">
    <el-form-item label="低余额提醒阈值" :error="error">
      <el-input
        v-model="low"
        aria-label="余额阈值人民币"
        inputmode="decimal"
        maxlength="32"
        placeholder="例如 10000.00"
        :disabled="disabled"
        @input="$emit('edited')"
        @blur="low = normalizeYuan(low) ?? low"
        ><template #prepend>¥</template></el-input
      >
    </el-form-item>
    <el-form-item label="恢复阈值（选填）">
      <el-input
        v-model="recovery"
        aria-label="恢复阈值人民币"
        inputmode="decimal"
        maxlength="32"
        placeholder="留空则使用提醒阈值"
        :disabled="disabled"
        @input="$emit('edited')"
        @blur="recovery = normalizeYuan(recovery) ?? recovery"
        ><template #prepend>¥</template></el-input
      >
    </el-form-item>
  </div>
  <p class="section-footnote">
    金额单位为元，最多两位小数；恢复阈值不得低于提醒阈值。
  </p>
</template>
