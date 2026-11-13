<script setup lang="ts">
import { ref } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { useRemote } from "@/composables/useRemote";
import type { Statistics } from "@/api/types";
import { number } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import TrendChart from "@/components/Console/TrendChart.vue";
const platform = usePlatformStore(),
  period = ref("今天");
const remote = useRemote<Statistics>(() =>
  platform.currentId
    ? `/platforms/${platform.currentId}/statistics?period=${encodeURIComponent(period.value)}`
    : ""
);
const columns = [
  { label: "模型", prop: "label", minWidth: 180 },
  { label: "消费记录", prop: "success_records", minWidth: 120 },
  { label: "错误记录", prop: "error_records", minWidth: 120 },
  {
    label: "消耗 quota",
    minWidth: 160,
    formatter: (r: { quota: number }) => number(r.quota)
  }
];
</script>
<template>
  <ConsolePage
    title="按需统计"
    description="仅在打开或查询时读取平台日志并内存汇总，不启动常驻历史采集"
    @refresh="remote.refresh"
    ><template #actions
      ><el-select v-model="period" aria-label="统计周期" style="width: 120px"
        ><el-option
          v-for="p in ['今天', '昨天', '近7天', '近30天']"
          :key="p"
          :value="p"
          :label="p" /></el-select
      ><el-button
        type="primary"
        :loading="remote.loading.value"
        @click="remote.refresh"
        >查询统计</el-button
      ></template
    ><el-alert
      v-if="remote.error.value"
      :title="remote.error.value"
      type="error"
      description="上游失败不等于零用量，请缩小时间范围或稍后重试"
      :closable="false"
    />
    <section class="surface">
      <div class="section-heading">
        <div>
          <h2>用量趋势</h2>
          <p>{{ period }} · quota 为原始额度单位，不等于人民币</p>
        </div>
      </div>
      <el-skeleton v-if="remote.loading.value" :rows="7" /><TrendChart
        v-else-if="remote.data.value"
        :data="remote.data.value"
      /><el-empty v-else description="尚无成功查询结果" />
    </section>
    <section v-if="remote.data.value" class="surface">
      <div class="section-heading"><h2>模型消耗排行</h2></div>
      <pure-table
        :columns="columns"
        :data="remote.data.value.ranking"
        row-key="key"
      />
      <p class="section-footnote">{{ remote.data.value.limitation }}</p>
    </section></ConsolePage
  >
</template>
