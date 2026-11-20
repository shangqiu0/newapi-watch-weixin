<script setup lang="ts">
import { useSettingsDraft } from "@/composables/useSettingsDraft";
import { computed, ref, watch } from "vue";
import { request, perform } from "@/api/console";
import { usePlatformStore } from "@/store/modules/platform";
import type { Currency } from "@/api/types";
import { validateCurrencyDraft } from "@/utils/moneyInput";
import CurrencyEditor from "@/components/Console/CurrencyEditor.vue";
const platform = usePlatformStore();
const currency = ref<Currency>({
  confirmed: false,
  quota_per_unit: "500000",
  usd_exchange_rate: "1",
  source: "manual"
});
const saving = ref(false);
const needsConfirmation = computed(() => {
  const previous = platform.current?.currency;
  return Boolean(
    previous?.confirmed &&
    !currency.value.confirmed &&
    (previous.quota_per_unit !== currency.value.quota_per_unit.trim() ||
      previous.usd_exchange_rate !== currency.value.usd_exchange_rate.trim())
  );
});
const dirty = useSettingsDraft();
watch(
  currency,
  () => {
    dirty.value = true;
  },
  { deep: true, flush: "sync" }
);
watch(
  () => platform.current,
  current => {
    if (dirty.value) return;
    currency.value = current?.currency
      ? { ...current.currency }
      : {
          confirmed: false,
          quota_per_unit: "500000",
          usd_exchange_rate: "1",
          source: "manual"
        };
    dirty.value = false;
  },
  { immediate: true }
);
async function save(): Promise<void> {
  if (!platform.currentId || saving.value || needsConfirmation.value) return;
  const id = platform.currentId;
  const error = validateCurrencyDraft(currency.value);
  if (error) return;
  saving.value = true;
  try {
    await perform(async () => {
      await request(`/platforms/${id}`, "PATCH", {
        currency: {
          ...currency.value,
          quota_per_unit: currency.value.quota_per_unit.trim(),
          usd_exchange_rate: currency.value.usd_exchange_rate.trim()
        }
      });
      if (platform.currentId === id) {
        dirty.value = false;
        await platform.load(id);
      }
    }, "人民币显示设置已保存；原始 quota 阈值保持不变");
  } finally {
    saving.value = false;
  }
}
</script>
<template>
  <div class="settings-panel">
    <section class="surface form-section">
      <h2>金额显示</h2>
      <p class="section-footnote">
        {{ platform.current?.name ?? "当前平台" }} ·
        余额、用量和用户监控阈值的人民币显示
      </p>
      <el-form label-position="top" @submit.prevent="save">
        <CurrencyEditor
          v-model="currency"
          :preview-path="
            platform.currentId
              ? `/platforms/${platform.currentId}/currency-previews`
              : undefined
          "
        />
        <p v-if="needsConfirmation" class="section-footnote" role="alert">
          参数已变更，请核对并重新确认后再保存。
        </p>
        <div class="form-footer">
          <el-button
            type="primary"
            native-type="submit"
            :loading="saving"
            :disabled="
              !platform.currentId ||
              !!validateCurrencyDraft(currency) ||
              needsConfirmation
            "
            >保存金额设置</el-button
          >
        </div>
      </el-form>
      <p class="section-footnote">
        显示金额 = quota ÷ 每 USD 对应的 quota ×
        人民币汇率。修改口径不重算已有用户监控的整数 quota
        阈值；上游账户使用各自的参数。
      </p>
    </section>
  </div>
</template>
