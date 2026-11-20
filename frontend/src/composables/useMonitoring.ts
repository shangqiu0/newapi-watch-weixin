import { ref, watch } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { useRemote } from "./useRemote";
import { ElMessage } from "element-plus";
import { request } from "@/api/console";
import type {
  BalanceRule,
  MonitoringConfig,
  MonitoringData
} from "@/api/types";
export type BalanceAmountEdit = {
  threshold_cny?: string;
  recovery_threshold_cny?: string | null;
};
export function useMonitoring() {
  const platform = usePlatformStore();
  const remote = useRemote<MonitoringData>(() =>
    platform.currentId ? `/platforms/${platform.currentId}/monitoring` : ""
  );
  const config = ref<MonitoringConfig | null>(null),
    saving = ref(false);
  watch(remote.data, data => {
    config.value = data ? JSON.parse(JSON.stringify(data.config)) : null;
  });
  async function save(
    balanceAmounts?: Record<string, BalanceAmountEdit>,
    balances?: BalanceRule[]
  ): Promise<boolean> {
    if (!config.value || saving.value) return false;
    saving.value = true;
    const id = platform.currentId;
    const payload = JSON.parse(JSON.stringify(config.value));
    if (balances) payload.balances = balances;
    payload.balances = ((payload.balances ?? []) as BalanceRule[]).map(
      rule => ({
        ...rule,
        threshold: rule.threshold_quota ?? rule.threshold,
        recovery_threshold:
          rule.recovery_threshold_quota ?? rule.recovery_threshold
      })
    );
    if (balanceAmounts) payload.balance_amounts = balanceAmounts;
    try {
      await request(`/platforms/${id}/monitoring`, "PUT", payload);
      if (platform.currentId === id) await remote.refresh();
      ElMessage.success("监控配置已保存");
      return true;
    } catch (error) {
      ElMessage.error(error instanceof Error ? error.message : "保存失败");
      return false;
    } finally {
      saving.value = false;
    }
  }
  return { ...remote, config, saving, save, platform };
}
