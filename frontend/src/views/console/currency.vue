<script setup lang="ts">
import { ref, watch } from "vue";
import { request, perform } from "@/api/console";
import { usePlatformStore } from "@/store/modules/platform";
import type { Currency } from "@/api/types";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import CurrencyEditor from "@/components/Console/CurrencyEditor.vue";
const platform = usePlatformStore();
const currency = ref<Currency>({
  confirmed: false,
  quota_per_unit: "500000",
  usd_exchange_rate: "1",
  source: "manual"
});
const saving = ref(false);
watch(
  () => platform.current,
  current => {
    currency.value = current?.currency
      ? { ...current.currency }
      : {
          confirmed: false,
          quota_per_unit: "500000",
          usd_exchange_rate: "1",
          source: "manual"
        };
  },
  { immediate: true }
);
async function save(): Promise<void> {
  if (!platform.currentId || saving.value) return;
  const id = platform.currentId;
  saving.value = true;
  try {
    await perform(async () => {
      await request(`/platforms/${id}`, "PATCH", { currency: currency.value });
      await platform.load();
    }, "人民币显示设置已保存；原始 quota 阈值保持不变");
  } finally {
    saving.value = false;
  }
}
</script>
<template>
  <ConsolePage
    title="金额显示"
    description="跟随当前平台的人民币额度口径；用户余额、消耗、告警与聊天回复使用同一套参数"
  >
    <section class="surface form-section">
      <h2>当前平台 · 人民币额度</h2>
      <el-alert
        title="截图上的 ¥ 不能单独确定倍率。请读取平台参数，或按该平台实际设置填写；未确认时不会把 quota 冒充人民币。"
        type="info"
        :closable="false"
      />
      <el-form label-position="top" @submit.prevent="save">
        <CurrencyEditor
          v-model="currency"
          :preview-path="
            platform.currentId
              ? `/platforms/${platform.currentId}/currency-previews`
              : undefined
          "
        />
        <div class="form-footer">
          <el-button
            type="primary"
            native-type="submit"
            :loading="saving"
            :disabled="!platform.currentId"
            >保存金额设置</el-button
          >
        </div>
      </el-form>
      <p class="section-footnote">
        修改倍率只改变显示，不静默重算既有 quota
        阈值。每个上游账户的倍率在「上游账户」中独立配置。渠道供应商 balance
        单位未确认，不自动套用此公式。
      </p>
    </section>
  </ConsolePage>
</template>
