import { watch, nextTick, onScopeDispose, type Ref } from "vue";
export function useMobileDrawer(
  root: Ref<HTMLElement | undefined>,
  isMobile: () => boolean,
  isOpen: () => boolean,
  close: () => void
): void {
  let trigger: HTMLElement | null = null;
  const clear = () => {
    const main = root.value?.querySelector<HTMLElement>(".main-container");
    if (main) main.inert = false;
    document.removeEventListener("keydown", keydown, true);
  };
  function keydown(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== "Tab") return;
    const panel = root.value?.querySelector<HTMLElement>(".sidebar-container");
    const items = Array.from(
      panel?.querySelectorAll<HTMLElement>(
        'a[href],button:not([disabled]),[tabindex="0"]'
      ) ?? []
    ).filter(el => el.getClientRects().length > 0);
    const first = items[0],
      last = items.at(-1);
    if (!first) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
  watch(
    [isMobile, isOpen],
    async ([mobile, open], old) => {
      clear();
      await nextTick();
      const panel =
        root.value?.querySelector<HTMLElement>(".sidebar-container");
      if (!panel) return;
      panel.inert = mobile && !open;
      if (mobile && !open) panel.setAttribute("aria-hidden", "true");
      else panel.removeAttribute("aria-hidden");
      if (mobile && open) {
        trigger = document.activeElement as HTMLElement;
        panel.setAttribute("role", "dialog");
        panel.setAttribute("aria-modal", "true");
        panel.setAttribute("aria-label", "导航菜单");
        const main = root.value?.querySelector<HTMLElement>(".main-container");
        if (main) main.inert = true;
        document.addEventListener("keydown", keydown, true);
        panel.querySelector<HTMLElement>("button,a[href]")?.focus();
      } else {
        panel.removeAttribute("role");
        panel.removeAttribute("aria-modal");
        if (old?.[0] && old?.[1] && trigger?.isConnected) trigger.focus();
      }
    },
    { immediate: true, flush: "post" }
  );
  onScopeDispose(clear);
}
