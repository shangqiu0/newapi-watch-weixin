<script setup lang="ts">
import { ref, watch } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { usePagedRemote } from "@/composables/usePagedRemote";
import type { AuditRecord, Page } from "@/api/types";
import { timeCN, number } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import StatusDot from "@/components/Console/StatusDot.vue";
const store = usePlatformStore(),
  page = ref(1),
  size = ref(20);
watch([() => store.currentId, size], () => {
  page.value = 1;
});
const { data, loading, error, total } = usePagedRemote<Page<AuditRecord>>(
  () =>
    store.currentId
      ? `/platforms/${store.currentId}/audits?page=${page.value}&page_size=${size.value}`
      : "",
  () => store.currentId
);
const columns = [
  {
    label: "时间",
    minWidth: 210,
    formatter: (r: AuditRecord) => timeCN(r.created_at, store.current?.timezone)
  },
  {
    label: "来源",
    minWidth: 130,
    formatter: (r: AuditRecord) =>
      r.source === "http"
        ? "管理台"
        : r.source === "bot"
          ? "企微机器人"
          : r.source
  },
  { label: "操作类别", prop: "action", minWidth: 160 },
  { label: "结果", slot: "outcome", minWidth: 140 },
  {
    label: "耗时",
    minWidth: 120,
    align: "right",
    formatter: (r: AuditRecord) => `${number(r.duration_ms)} ms`
  }
];
</script>
<template>
  <ConsolePage
    title="操作审计"
    description="只记录来源、类别、结果与耗时，不存储原始问题、姓名或密钥"
    ><section class="surface">
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        :closable="false"
      /><pure-table
        :data="data?.items ?? []"
        :columns="columns"
        :loading="loading"
        row-key="id"
        ><template #outcome="{ row }"
          ><StatusDot
            :text="
              row.outcome === 'success'
                ? '成功'
                : row.outcome === 'upstream_error'
                  ? '上游错误'
                  : row.outcome === 'invalid_input'
                    ? '输入无效'
                    : row.outcome
            "
            :tone="
              row.outcome === 'success' ? 'ok' : 'warn'
            " /></template></pure-table
      ><el-pagination
        v-model:current-page="page"
        v-model:page-size="size"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total,sizes,prev,pager,next,jumper"
      /></section
  ></ConsolePage>
</template>
