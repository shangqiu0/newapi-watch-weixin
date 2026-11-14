<script setup lang="ts">
import { ref, watch } from "vue";
import { useIntervalFn } from "@vueuse/core";
import { usePlatformStore } from "@/store/modules/platform";
import { usePagedRemote } from "@/composables/usePagedRemote";
import { request, perform } from "@/api/console";
import type { QueryTask, Page } from "@/api/types";
import { number, timeCN } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import TrendChart from "@/components/Console/TrendChart.vue";
const store = usePlatformStore(),
  page = ref(1),
  size = ref(20),
  selected = ref<QueryTask | null>(null),
  busy = ref("");
const remote = usePagedRemote<Page<QueryTask>>(
  () =>
    store.currentId
      ? `/platforms/${store.currentId}/query-tasks?page=${page.value}&page_size=${size.value}`
      : "",
  () => store.currentId
);
const labels: Record<QueryTask["status"], string> = {
  queued: "等待执行",
  running: "正在读取",
  waiting: "限流等待",
  paused: "已暂停",
  failed: "执行失败",
  complete: "已完成",
  cancelled: "已取消"
};
watch([() => store.currentId, size], () => {
  page.value = 1;
  selected.value = null;
});
useIntervalFn(() => {
  if (
    !remote.loading.value &&
    remote.data.value?.items.some(t =>
      ["queued", "running", "waiting"].includes(t.status)
    )
  )
    void remote.refresh();
}, 5000);
async function action(
  task: QueryTask,
  kind: "retry" | "cancel"
): Promise<void> {
  if (busy.value) return;
  const platform = store.currentId;
  busy.value = task.id;
  try {
    await perform(
      async () => {
        await request(
          `/platforms/${platform}/query-tasks/${task.id}${kind === "retry" ? "/retries" : ""}`,
          kind === "retry" ? "POST" : "DELETE"
        );
        if (platform === store.currentId) await remote.refresh();
      },
      kind === "retry" ? "任务已继续" : "任务已取消"
    );
  } finally {
    busy.value = "";
  }
}
const columns = [
  { label: "查询任务", slot: "name", minWidth: 260 },
  { label: "状态", slot: "status", width: 130 },
  {
    label: "读取记录",
    minWidth: 120,
    align: "right",
    formatter: (r: QueryTask) => number(r.progress.records_read)
  },
  {
    label: "请求次数",
    width: 110,
    align: "right",
    formatter: (r: QueryTask) => number(r.progress.total_requests)
  },
  { label: "说明", prop: "error", minWidth: 220 },
  { label: "操作", slot: "action", fixed: "right", width: 220 }
];
</script>
<template>
  <ConsolePage
    title="查询任务"
    description="精确历史查询在后台分批读取，进度落盘；优先保障渠道与余额监控，不长期保存原始业务日志"
    @refresh="remote.refresh"
  >
    <template #actions
      ><el-button :loading="remote.loading.value" @click="remote.refresh"
        >刷新进度</el-button
      ></template
    >
    <el-alert
      v-if="remote.error.value"
      :title="remote.error.value"
      type="error"
      :closable="false"
    />
    <section class="surface">
      <div class="section-heading">
        <div>
          <h2>平台查询队列</h2>
          <p>
            任务按创建时的时间范围执行；暂停或未完成时不发布部分统计。可在按需统计页发起查询。
          </p>
        </div>
      </div>
      <pure-table
        :data="remote.data.value?.items ?? []"
        :columns="columns"
        :loading="remote.loading.value"
        row-key="id"
      >
        <template #name="{ row }"
          ><span class="mono">{{ row.id }}</span>
          <p class="section-footnote">
            {{ timeCN(row.query.start, store.current?.timezone) }} —
            {{ timeCN(row.query.end, store.current?.timezone) }} ·
            {{ row.query.dimension }}
          </p></template
        >
        <template #status="{ row }"
          ><el-tag
            :type="
              row.status === 'complete'
                ? 'success'
                : ['failed', 'paused'].includes(row.status)
                  ? 'warning'
                  : 'info'
            "
            effect="plain"
            >{{ labels[row.status as QueryTask["status"]] }}</el-tag
          ></template
        >
        <template #action="{ row }"
          ><el-button
            v-if="['paused', 'failed'].includes(row.status)"
            link
            type="primary"
            :loading="busy === row.id"
            @click="action(row, 'retry')"
            >继续任务</el-button
          ><el-button
            v-if="
              ['queued', 'running', 'waiting', 'paused'].includes(row.status)
            "
            link
            type="danger"
            :disabled="!!busy"
            @click="action(row, 'cancel')"
            >取消</el-button
          ><el-button
            v-if="row.status === 'complete'"
            link
            type="primary"
            @click="selected = row"
            >查看结果</el-button
          ></template
        > </pure-table
      ><el-pagination
        v-model:current-page="page"
        v-model:page-size="size"
        :page-sizes="[20, 50, 100]"
        :total="remote.total.value"
        layout="total,sizes,prev,pager,next,jumper"
      />
    </section>
    <section v-if="selected?.result" class="surface">
      <div class="section-heading">
        <h2>完整查询结果</h2>
        <el-button link @click="selected = null">收起</el-button>
      </div>
      <p>
        消费记录 {{ number(selected.result.summary.success_records) }} ·
        错误记录 {{ number(selected.result.summary.error_records) }} · 消耗
        {{ number(selected.result.summary.quota) }} quota
      </p>
      <TrendChart :data="selected.result" /><pure-table
        :data="selected.result.ranking"
        :columns="[
          { label: '对象', prop: 'label' },
          { label: '消耗 quota', prop: 'quota' },
          { label: '消费记录', prop: 'success_records' },
          { label: '错误记录', prop: 'error_records' }
        ]"
      />
      <p class="section-footnote">{{ selected.result.limitation }}</p>
    </section>
  </ConsolePage>
</template>
