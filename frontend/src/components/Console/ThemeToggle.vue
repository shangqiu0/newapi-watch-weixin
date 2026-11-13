<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from "vue";
import { Sun, Moon } from "lucide-vue-next";
import { useDataThemeChange } from "@/layout/hooks/useDataThemeChange";
const { dataTheme, dataThemeChange, setEpThemeColor } = useDataThemeChange();
const dark = ref(document.documentElement.classList.contains("dark"));
let explicit = false;
const system = matchMedia("(prefers-color-scheme: dark)");
function apply(value: boolean) {
  dark.value = value;
  dataTheme.value = value;
  dataThemeChange(value ? "dark" : "light");
  setEpThemeColor(value ? "#4ab3a5" : "#237a73");
  document.documentElement.dataset.theme = value ? "dark" : "light";
}
function toggle() {
  explicit = true;
  apply(!dark.value);
  try {
    localStorage.setItem("newapi-watch-theme", dark.value ? "dark" : "light");
  } catch {}
}
const changed = (e: MediaQueryListEvent) => {
  if (!explicit) apply(e.matches);
};
onMounted(() => {
  let saved: string | null = null;
  try {
    saved = localStorage.getItem("newapi-watch-theme");
  } catch {}
  explicit = saved === "dark" || saved === "light";
  apply(explicit ? saved === "dark" : system.matches);
  system.addEventListener("change", changed);
});
onBeforeUnmount(() => system.removeEventListener("change", changed));
</script>
<template>
  <el-button
    text
    circle
    :aria-label="dark ? '切换亮白主题' : '切换暗黑主题'"
    @click="toggle"
    ><Sun v-if="dark" :size="18" /><Moon v-else :size="18"
  /></el-button>
</template>
