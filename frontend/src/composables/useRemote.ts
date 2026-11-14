import { ref, watch, onScopeDispose, shallowRef } from "vue";
import { request } from "@/api/console";
export function useRemote<T>(
  path: () => string,
  method: "GET" | "POST" = "GET"
) {
  const data = shallowRef<T | null>(null),
    loading = ref(false),
    error = ref("");
  let generation = 0,
    controller: AbortController | null = null;
  async function refresh() {
    const url = path();
    const seq = ++generation;
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    error.value = "";
    data.value = null;
    if (!url) {
      loading.value = false;
      return;
    }
    loading.value = true;
    try {
      const result = await request<T>(url, method, undefined, signal);
      if (seq === generation) data.value = result;
    } catch (e) {
      if (seq === generation && !signal.aborted)
        error.value = e instanceof Error ? e.message : "读取失败";
    } finally {
      if (seq === generation) loading.value = false;
    }
  }
  watch(path, () => void refresh(), { immediate: true });
  onScopeDispose(() => {
    generation++;
    controller?.abort();
  });
  return { data, loading, error, refresh };
}
