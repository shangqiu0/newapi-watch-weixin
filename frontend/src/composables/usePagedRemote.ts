import { ref, watch } from "vue";
import { useRemote } from "./useRemote";
/** Keep the last successful total while a page is loading; never reuse it across platforms. */
export function usePagedRemote<T extends { total: number }>(
  path: () => string,
  scope: () => string
) {
  const total = ref(0);
  watch(
    scope,
    () => {
      total.value = 0;
    },
    { flush: "sync" }
  );
  const remote = useRemote<T>(path);
  watch(
    remote.data,
    value => {
      if (value) total.value = value.total;
    },
    { flush: "sync" }
  );
  return { ...remote, total };
}
