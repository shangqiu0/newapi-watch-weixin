<script setup lang="ts">
import HistoryActions from "@/components/Console/HistoryActions.vue";
import { usePageBounds } from "@/composables/usePageBounds";
import { ref, watch } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { usePagedRemote } from "@/composables/usePagedRemote";
import type { Page, AlertRecord } from "@/api/types";
import { timeCN, alertState } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import StatusDot from "@/components/Console/StatusDot.vue";
const store = usePlatformStore(),
  page = ref(1),
  size = ref(20);
watch([() => store.currentId, size], () => {
  page.value = 1;
});
const { data, loading, error, refresh, total } = usePagedRemote<
  Page<AlertRecord>
>(
  () =>
    store.currentId
      ? `/platforms/${store.currentId}/alerts?page=${page.value}&page_size=${size.value}`
      : "",
  () => store.currentId
);
const selected = ref<AlertRecord | null>(null),
  drawer = ref(false);
const selectedRows = ref<AlertRecord[]>([]);
watch([() => store.currentId, page, size], () => {
  selectedRows.value = [];
});
usePageBounds(total, page, size);
const columns = [
  {
    type: "selection",
    width: 44,
    selectable: (row: AlertRecord) => ["resolved", "event"].includes(row.state)
  },
  { label: "告警", slot: "title", minWidth: 320 },
  { label: "类别与状态", slot: "state", minWidth: 150 },
  {
    label: "更新时间",
    minWidth: 200,
    formatter: (r: AlertRecord) => timeCN(r.updated_at, store.current?.timezone)
  },
  { label: "操作", slot: "action", width: 110, fixed: "right" }
];
</script>
<template>
  <ConsolePage
    title="告警记录"
    description="状态告警与请求错误事件分开记录，不将停止报错当作故障恢复"
    @refresh="refresh"
    ><template #actions
      ><el-button @click="$router.push('/console/policies')"
        >编辑监控策略</el-button
      ></template
    ><el-alert v-if="error" :title="error" type="error" :closable="false" />
    <section class="surface">
      <div class="table-toolbar">
        <span class="toolbar-meta"
          >已选择 {{ selectedRows.length }} 条 · 活动告警不可删除</span
        ><HistoryActions
          kind="alerts"
          :ids="selectedRows.map(r => r.id)"
          @changed="
            selectedRows = [];
            refresh();
          "
        />
      </div>
      <pure-table
        :data="data?.items ?? []"
        :columns="columns"
        :loading="loading"
        row-key="id"
        @selection-change="rows => (selectedRows = rows)"
        ><template #title="{ row }"
          ><el-button
            class="alert-title-link"
            link
            type="primary"
            @click="
              selected = row;
              drawer = true;
            "
            >{{ row.title.replace(/\n/g, " · ") }}</el-button
          ></template
        ><template #action="{ row }"
          ><HistoryActions
            v-if="['resolved', 'event'].includes(row.state)"
            kind="alerts"
            :ids="[row.id]"
            label="删除"
            @changed="refresh"
          /><span v-else class="muted">活动告警</span></template
        ><template #state="{ row }"
          ><StatusDot
            :text="alertState(row).text"
            :tone="alertState(row).tone" /></template></pure-table
      ><el-pagination
        v-model:current-page="page"
        v-model:page-size="size"
        :page-sizes="[20, 50, 100]"
        :total="total"
        layout="total,sizes,prev,pager,next,jumper"
      />
    </section>
    <el-drawer v-model="drawer" title="告警详情" size="min(560px, 100vw)"
      ><template v-if="selected"
        ><StatusDot
          :text="alertState(selected).text"
          :tone="alertState(selected).tone"
        />
        <pre class="notification-preview">{{ selected.title }}</pre>
        <p class="section-footnote">
          更新于 {{ timeCN(selected.updated_at, store.current?.timezone) }}
        </p></template
      ></el-drawer
    ></ConsolePage
  >
</template>
