<script setup lang="ts">
import { ref, computed } from "vue";
import { useRouter } from "vue-router";
import { usePlatformStore } from "@/store/modules/platform";
import { useRemote } from "@/composables/useRemote";
import type { Statistics, PendingStatistics, Rank } from "@/api/types";
import { number } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import TrendChart from "@/components/Console/TrendChart.vue";
const platform = usePlatformStore(),
  router = useRouter(),
  period = ref("今天"),
  dimension = ref("model"),
  exact = ref(false);
const remote = useRemote<Statistics | PendingStatistics>(
  () =>
    platform.currentId
      ? `/platforms/${platform.currentId}/statistics-queries?period=${encodeURIComponent(period.value)}&dimension=${dimension.value}&exact=${exact.value}`
      : "",
  "POST"
);
const pending = computed(() =>
  remote.data.value && "pending" in remote.data.value ? remote.data.value : null
);
const snapshot = computed(() =>
  remote.data.value && !("pending" in remote.data.value)
    ? remote.data.value
    : null
);
const columns = [
  { label: "对象", prop: "label", minWidth: 180 },
  { label: "消费记录", prop: "success_records", minWidth: 120 },
  {
    label: "错误记录",
    minWidth: 120,
    formatter: (r: Rank) =>
      snapshot.value?.unavailable_metrics?.includes("error_records")
        ? "未提供"
        : number(r.error_records)
  },
  {
    label: "消耗 quota",
    minWidth: 160,
    formatter: (r: Rank) => number(r.quota)
  }
];
</script>
<template>
  <ConsolePage
    title="按需统计"
    description="优先读取平台已落盘汇总；精确统计在后台分批执行，不让聊天连接等待全量日志"
    @refresh="remote.refresh"
  >
    <template #actions
      ><el-select v-model="period" aria-label="统计周期" style="width: 120px"
        ><el-option
          v-for="p in ['今天', '昨天', '近7天', '近30天']"
          :key="p"
          :value="p"
          :label="p" /></el-select
      ><el-select v-model="dimension" aria-label="统计维度" style="width: 110px"
        ><el-option label="模型" value="model" /><el-option
          label="用户"
          value="user" /><el-option label="渠道" value="channel" /></el-select
      ><el-checkbox v-model="exact">精确统计任务</el-checkbox
      ><el-button :loading="remote.loading.value" @click="remote.refresh"
        >查询统计</el-button
      ></template
    >
    <el-alert
      v-if="remote.error.value"
      :title="remote.error.value"
      type="error"
      description="未取得数据不代表零用量；检查权限或改用精确统计任务"
      :closable="false"
    />
    <section v-if="pending" class="surface">
      <div class="section-heading"><h2>查询任务已受理</h2></div>
      <p class="mono">{{ pending.task.id }}</p>
      <p>{{ pending.limitation }}</p>
      <el-button type="primary" @click="router.push('/console/tasks')"
        >查看查询任务</el-button
      >
    </section>
    <section v-else class="surface">
      <div class="section-heading">
        <div>
          <h2>用量趋势</h2>
          <p>
            {{ period }} ·
            {{
              snapshot?.source === "newapi_hourly_aggregate"
                ? "上游已落盘汇总，非实时完整日志"
                : "quota 为原始额度单位"
            }}
          </p>
        </div>
      </div>
      <el-skeleton v-if="remote.loading.value" :rows="7" /><TrendChart
        v-else-if="snapshot?.series.length"
        :data="snapshot"
      /><el-empty
        v-else
        :description="
          snapshot
            ? '此接口未提供时间轴或没有可确认的趋势点'
            : '尚无成功查询结果'
        "
      />
    </section>
    <section v-if="snapshot" class="surface">
      <div class="section-heading"><h2>消耗排行</h2></div>
      <pure-table :columns="columns" :data="snapshot.ranking" row-key="key" />
      <p class="section-footnote">{{ snapshot.limitation }}</p>
    </section>
  </ConsolePage>
</template>
