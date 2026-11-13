<script setup lang="ts">
import { computed } from "vue";
import { useMonitorRun } from "@/composables/useMonitorRun";
import MonitorChecks from "@/components/Console/MonitorChecks.vue";
import { useMonitoring } from "@/composables/useMonitoring";
import PolicyTargets from "@/components/Console/PolicyTargets.vue";
import ConsolePage from "@/components/Console/ConsolePage.vue";
const state = useMonitoring(),
  config = state.config;
const recheck = useMonitorRun();
const reportChecks = computed(
  () =>
    state.data.value?.execution?.checks.filter(
      c => c.id === "daily" || c.id === "weekly"
    ) ?? []
);
const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
</script>
<template>
  <ConsolePage
    title="报告调度"
    description="日报与周报分别配置启停、发送时间与统计图，按绑定平台时区执行"
    @refresh="state.refresh"
    ><template #actions
      ><el-popconfirm
        title="复查正式监控并允许本期暂停报告再试一次？可能触发正式通知"
        @confirm="recheck.run"
        ><template #reference
          ><el-button
            :disabled="!state.platform.current?.enabled"
            :loading="recheck.submitting.value"
            >立即复查</el-button
          ></template
        ></el-popconfirm
      >
      <el-button
        type="primary"
        :loading="state.saving.value"
        :disabled="!config"
        @click="state.save"
        >保存报告设置</el-button
      ></template
    ><el-alert
      v-if="state.error.value"
      :title="state.error.value"
      type="error"
      :closable="false"
    /><el-alert
      :title="`调度时区：${state.platform.current?.timezone ?? '平台时区'}`"
      description="日报统计前一自然日；周报统计发送前七个完整自然日。到时分批读取并保存汇总进度，不保存原始业务日志；默认失败3次后暂停本期，不无限重试，不发送不完整报告"
      type="info"
      :closable="false"
    />
    <section v-if="reportChecks.length" class="surface">
      <div class="section-heading">
        <div>
          <h2>报告执行进度</h2>
          <p>
            已完成页会续跑，完整统计才进入通知队列；暂停时可立即复查手动重试
          </p>
        </div>
      </div>
      <MonitorChecks
        :checks="reportChecks"
        :timezone="state.platform.current?.timezone"
      />
    </section>
    <div v-if="config" class="report-grid">
      <section
        v-for="kind in ['daily', 'weekly'] as const"
        :key="kind"
        class="surface"
      >
        <div class="section-heading">
          <div>
            <h2>{{ kind === "daily" ? "每日总结" : "每周总结" }}</h2>
            <p>
              {{
                kind === "daily"
                  ? "前一自然日消费与异常概览"
                  : "前七个完整自然日汇总与排行"
              }}
            </p>
          </div>
          <el-switch
            v-model="config[kind].enabled"
            :active-text="config[kind].enabled ? '已启用' : '已停用'"
          />
        </div>
        <el-form label-position="top"
          ><el-form-item v-if="kind === 'weekly'" label="每周发送日"
            ><el-select v-model="config.weekly.weekday"
              ><el-option
                v-for="(day, index) in days"
                :key="day"
                :label="day"
                :value="index" /></el-select></el-form-item
          ><el-form-item label="发送时间"
            ><div class="time-controls">
              <el-input-number
                v-model="config[kind].hour"
                :min="0"
                :max="23"
                aria-label="发送小时"
              /><span>时</span
              ><el-input-number
                v-model="config[kind].minute"
                :min="0"
                :max="59"
                aria-label="发送分钟"
              /><span>分</span>
            </div></el-form-item
          ><el-form-item label="通知 Webhook"
            ><PolicyTargets v-model="config[kind].binding_ids" :kind="kind"
          /></el-form-item>
          <div class="form-grid">
            <el-form-item label="每轮最多读取页数"
              ><el-input-number
                v-model="config[kind].pages_per_run"
                :min="1"
                :max="10" /></el-form-item
            ><el-form-item label="本期最多失败次数"
              ><el-input-number
                v-model="config[kind].max_failures"
                :min="1"
                :max="10" /></el-form-item
            ><el-form-item label="失败重试间隔（秒）"
              ><el-input-number
                v-model="config[kind].retry_seconds"
                :min="30"
                :max="86400"
            /></el-form-item>
          </div>
          <el-form-item label="附带统计图"
            ><el-switch v-model="config[kind].with_image" /></el-form-item
        ></el-form>
        <div class="section-footnote">
          通过本平台已启用且订阅{{ kind === "daily" ? "日报" : "周报" }}的群
          Webhook 发送；重启后同一期报告不重复入队
        </div>
      </section>
    </div></ConsolePage
  >
</template>
