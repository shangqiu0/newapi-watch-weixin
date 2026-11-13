import { it, expect } from "vitest";
import { effectScope, ref, nextTick } from "vue";
import { useMobileDrawer } from "../src/composables/useMobileDrawer";
it("移动导航隔离背景、Escape关闭并恢复触发按钮焦点", async () => {
  document.body.innerHTML =
    '<button id="trigger">菜单</button><div id="root"><div class="sidebar-container"><button>关闭导航</button><a href="/console/overview">总览</a></div><main class="main-container"><button>背景操作</button></main></div>';
  const trigger = document.querySelector<HTMLButtonElement>("#trigger");
  trigger.focus();
  const root = ref(document.querySelector<HTMLElement>("#root")),
    mobile = ref(true),
    open = ref(false),
    scope = effectScope();
  scope.run(() =>
    useMobileDrawer(
      root,
      () => mobile.value,
      () => open.value,
      () => {
        open.value = false;
      }
    )
  );
  await nextTick();
  await nextTick();
  open.value = true;
  await nextTick();
  await nextTick();
  expect(root.value.querySelector<HTMLElement>("main").inert).toBe(true);
  expect(
    root.value.querySelector(".sidebar-container").getAttribute("aria-modal")
  ).toBe("true");
  document.dispatchEvent(
    new KeyboardEvent("keydown", { key: "Escape", bubbles: true })
  );
  await nextTick();
  await nextTick();
  expect(open.value).toBe(false);
  expect(root.value.querySelector<HTMLElement>("main").inert).toBe(false);
  expect(document.activeElement).toBe(trigger);
  scope.stop();
  document.body.innerHTML = "";
});
