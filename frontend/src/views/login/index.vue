<script setup lang="ts">
import { reactive, ref } from "vue";
import { useRouter, useRoute } from "vue-router";
import { Activity, ArrowRight, ShieldCheck } from "lucide-vue-next";
import { useSessionStore } from "@/store/modules/session";
import { safeRedirect } from "@/utils/format";
import ThemeToggle from "@/components/Console/ThemeToggle.vue";
const session = useSessionStore(),
  router = useRouter(),
  route = useRoute();
const values = reactive({ username: "", password: "" }),
  busy = ref(false),
  error = ref("");
async function login() {
  error.value = "";
  busy.value = true;
  try {
    await session.login(values.username, values.password);
    values.password = "";
    await router.replace(safeRedirect(route.query.redirect));
  } catch (e) {
    error.value = e instanceof Error ? e.message : "登录失败";
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <main class="login-page">
    <header>
      <a class="console-brand" href="/"
        ><Activity :size="22" />NewAPI <span>Watch</span></a
      ><ThemeToggle />
    </header>
    <div class="login-main">
      <div class="login-panel">
        <div class="context-label">监控管理工作台</div>
        <h1>登录控制台</h1>
        <p>查看渠道、追踪异常、管理平台通知</p>
        <el-form label-position="top" @submit.prevent="login"
          ><el-form-item label="管理账号"
            ><el-input
              v-model="values.username"
              autocomplete="username"
              placeholder="请输入部署时分配的账号"
              size="large"
              required /></el-form-item
          ><el-form-item label="密码"
            ><el-input
              v-model="values.password"
              autocomplete="current-password"
              type="password"
              show-password
              placeholder="请输入密码"
              size="large"
              required /></el-form-item
          ><el-alert
            v-if="error"
            :title="error"
            type="error"
            :closable="false"
            role="alert" /><el-button
            class="login-submit"
            type="primary"
            native-type="submit"
            size="large"
            :loading="busy"
            >登录管理台<ArrowRight :size="16" /></el-button
        ></el-form>
        <div class="login-hint">
          <ShieldCheck :size="16" /><span
            >唯一管理账号，无需注册<br />初始凭证位于部署目录
            initial-account.txt</span
          >
        </div>
      </div>
    </div>
    <footer>NewAPI Watch · 独立监控服务</footer>
  </main>
</template>
