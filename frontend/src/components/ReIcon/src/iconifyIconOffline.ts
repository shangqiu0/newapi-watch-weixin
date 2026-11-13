import { h, defineComponent, type PropType, type Component } from "vue";
import { Icon as IconifyIcon } from "@iconify/vue/dist/offline";
import type { IconifyIcon as IconData } from "@iconify/types";
export default defineComponent({
  name: "IconifyIconOffline",
  props: {
    icon: {
      type: [String, Object, Function] as PropType<
        string | IconData | Component
      >,
      default: null
    }
  },
  render() {
    const icon = this.icon;
    if (!icon) return null;
    if (
      typeof icon === "function" ||
      (typeof icon === "object" && "render" in icon)
    )
      return h(icon as Component, this.$attrs);
    return h(IconifyIcon, {
      ...this.$attrs,
      icon: icon as string | IconData,
      "aria-hidden": true
    });
  }
});
