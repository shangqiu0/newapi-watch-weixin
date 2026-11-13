<script setup lang="ts">
import { ref } from "vue";
import { Menu, LogOut, Plus, Settings2, ShieldCheck } from "lucide-vue-next";
import { useRouter } from "vue-router";
import { usePlatformStore } from "@/store/modules/platform";
import { useSessionStore } from "@/store/modules/session";
import { useAppStoreHook } from "@/store/modules/app";
import { perform } from "@/api/console";
import ThemeToggle from "@/components/Console/ThemeToggle.vue";
import PlatformEditor from "@/components/Console/PlatformEditor.vue";
const platform = usePlatformStore(),
  session = useSessionStore(),
  app = useAppStoreHook(),
  router = useRouter();
const add = ref(false),
  edit = ref(false);
const logout = () =>
  perform(async () => {
    await session.logout();
    await router.replace("/login");
  });
</script>
<template>
  <div class="navbar console-navbar">
    <div class="nav-left">
      <el-button text circle aria-label="切换导航" @click="app.toggleSideBar()"
        ><Menu :size="18" /></el-button
      ><span class="nav-divider" /><span class="nav-workspace">当前平台</span
      ><el-select
        :model-value="platform.currentId"
        aria-label="当前监控平台"
        placeholder="选择平台"
        style="width: 220px"
        @change="id => perform(() => platform.select(id))"
        ><el-option
          v-for="item in platform.list"
          :key="item.id"
          :label="item.name"
          :value="item.id" /></el-select
      ><el-button text circle aria-label="新增平台" @click="add = true"
        ><Plus :size="18" /></el-button
      ><el-button
        text
        circle
        aria-label="编辑当前平台"
        title="编辑当前平台"
        :disabled="!platform.current"
        @click="edit = true"
        ><Settings2 :size="18" /></el-button
      ><el-button
        text
        class="platform-management-link"
        @click="router.push('/console/platforms')"
        >平台管理</el-button
      >
    </div>
    <div class="nav-right">
      <span class="scope-note"><ShieldCheck :size="14" />平台隔离</span
      ><span v-if="session.demo" class="context-label">离线演示</span
      ><ThemeToggle /><span class="nav-account">{{ session.username }}</span
      ><el-button text circle aria-label="退出登录" @click="logout"
        ><LogOut :size="18"
      /></el-button>
    </div>
    <PlatformEditor v-model="add" /><PlatformEditor
      v-model="edit"
      :platform="platform.current"
    />
  </div>
</template>

<style scoped>
@media (width <= 760px) {
  .platform-management-link {
    display: none;
  }
}
</style>
