<script setup lang="ts">
import { ref, provide, onUnmounted, onMounted, watch } from "vue";
import {
  useRoute,
  useRouter,
  onBeforeRouteLeave,
  onBeforeRouteUpdate
} from "vue-router";
import { ElMessageBox } from "element-plus";
import {
  Settings2,
  Database,
  Coins,
  Plug,
  ShieldCheck,
  Cable
} from "lucide-vue-next";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import ConnectionSettings from "@/components/Console/Settings/ConnectionSettings.vue";
import CurrencySettings from "@/components/Console/Settings/CurrencySettings.vue";
import ModelSettings from "@/components/Console/Settings/ModelSettings.vue";
import DataManagement from "@/components/Console/Settings/DataManagement.vue";
import RunRecords from "@/components/Console/Settings/RunRecords.vue";
import { registerPlatformGuard } from "@/utils/platformGuard";
const route = useRoute(),
  router = useRouter(),
  dirty = ref(false);
provide("settingsDirty", dirty);
const tabs = [
  { key: "connection", label: "接入与时间", icon: Cable },
  { key: "currency", label: "金额显示", icon: Coins },
  { key: "model", label: "模型服务", icon: Plug },
  { key: "data", label: "数据管理", icon: Database },
  { key: "records", label: "运行记录", icon: ShieldCheck }
];
const active = ref(
  tabs.some(t => t.key === route.query.tab)
    ? String(route.query.tab)
    : "connection"
);
watch(
  () => route.query.tab,
  value => {
    if (tabs.some(t => t.key === value)) active.value = String(value);
  }
);
async function leave(): Promise<boolean> {
  if (!dirty.value) return true;
  try {
    await ElMessageBox.confirm(
      "当前分区有未保存的更改，是否放弃？",
      "未保存的设置",
      {
        type: "warning",
        confirmButtonText: "放弃更改",
        cancelButtonText: "继续编辑"
      }
    );
    dirty.value = false;
    return true;
  } catch {
    return false;
  }
}
const unregister = registerPlatformGuard(leave);
onBeforeRouteLeave(leave);
onBeforeRouteUpdate((to, from) =>
  to.fullPath !== from.fullPath ? leave() : true
);
async function switchTab(next: string | number): Promise<boolean> {
  if (!(await leave())) return false;
  await router.replace({ query: { ...route.query, tab: String(next) } });
  return true;
}
function beforeUnload(event: BeforeUnloadEvent): void {
  if (dirty.value) {
    event.preventDefault();
    event.returnValue = "";
  }
}
onMounted(() => window.addEventListener("beforeunload", beforeUnload));
onUnmounted(() => {
  unregister();
  window.removeEventListener("beforeunload", beforeUnload);
});
</script>
<template>
  <ConsolePage
    title="平台设置"
    description="接入、金额、模型与数据维护，在当前平台独立生效。"
  >
    <template #actions
      ><span v-if="dirty" class="draft-state"
        ><span class="status-dot warn" />有未保存更改</span
      ></template
    >
    <div class="settings-workspace">
      <div class="settings-context">
        <Settings2 :size="18" />
        <div>
          <strong>配置中心</strong>
          <p>分区保存，互不覆盖</p>
        </div>
        <span class="section-footnote">敏感凭证加密保存</span>
      </div>
      <el-tabs v-model="active" class="settings-tabs" :before-leave="switchTab">
        <el-tab-pane v-for="tab in tabs" :key="tab.key" :name="tab.key"
          ><template #label
            ><span class="settings-tab-label"
              ><component :is="tab.icon" :size="16" />{{ tab.label }}</span
            ></template
          ></el-tab-pane
        >
      </el-tabs>
      <ConnectionSettings v-if="active === 'connection'" />
      <CurrencySettings v-else-if="active === 'currency'" />
      <ModelSettings v-else-if="active === 'model'" />
      <DataManagement v-else-if="active === 'data'" />
      <RunRecords v-else />
    </div>
  </ConsolePage>
</template>
