<script setup lang="ts">
import { useMonitoring } from "@/composables/useMonitoring";
import PolicyTargets from "@/components/Console/PolicyTargets.vue";
import ConsolePage from "@/components/Console/ConsolePage.vue";
const state = useMonitoring(),
  config = state.config;
const days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
</script>
<template>
  <ConsolePage
    title="报告调度"
    description="日报与周报分别配置启停、发送时间与统计图，按绑定平台时区执行"
    @refresh="state.refresh"
    ><template #actions
      ><el-button
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
      description="日报统计前一自然日；周报统计发送前七个完整自然日。仅在生成时读取平台数据，内存汇总不持续保存原始日志，上游失败会保留失败状态并重试，不发送伪造的零报告"
      type="info"
      :closable="false"
    />
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
            ><PolicyTargets
              v-model="config[kind].binding_ids"
              :kind="kind" /></el-form-item
          ><el-form-item label="附带统计图"
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
