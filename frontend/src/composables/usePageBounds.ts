import { watch, type Ref } from "vue";
export function usePageBounds(
  total: Ref<number>,
  page: Ref<number>,
  size: Ref<number>
): void {
  watch([total, size], ([count, limit]) => {
    if (page.value > Math.max(1, Math.ceil(count / limit)))
      page.value = Math.max(1, Math.ceil(count / limit));
  });
}
