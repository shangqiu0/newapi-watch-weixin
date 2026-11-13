import { ref } from "vue";
import { request } from "@/api/console";
import { ElMessage } from "element-plus";
import { usePlatformStore } from "@/store/modules/platform";
import type { MonitorRun } from "@/api/types";
export function useMonitorRun() {
  const platform = usePlatformStore();
  const submitting = ref(false);
  async function run(): Promise<void> {
    if (submitting.value || !platform.current?.enabled) return;
    submitting.value = true;
    try {
      await request<MonitorRun>(
        `/platforms/${platform.currentId}/monitor-runs`,
        "POST",
        { request_id: crypto.randomUUID() }
      );
      ElMessage.success("复查已排队，将按正式策略判断与通知");
      await platform.refresh();
    } catch (error) {
      ElMessage.error(error instanceof Error ? error.message : "复查失败");
    } finally {
      submitting.value = false;
    }
  }
  return { submitting, run };
}
