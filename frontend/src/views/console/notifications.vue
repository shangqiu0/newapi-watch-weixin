<script setup lang="ts">
import { ref, watch } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { usePagedRemote } from "@/composables/usePagedRemote";
import { request, perform } from "@/api/console";
import { timeCN } from "@/utils/format";
import type { Page } from "@/api/types";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import StatusDot from "@/components/Console/StatusDot.vue";
interface Delivery {
  id: number;
  kind: string;
  status: string;
  delivery_method?: string;
  attempts: number;
  last_error: string;
  next_at: number;
  created_at: number;
  updated_at: number;
  binding_ids: string[];
}
const platform = usePlatformStore(),
  page = ref(1),
  size = ref(20),
  retrying = ref<number | null>(null);
watch([() => platform.currentId, size], () => {
  page.value = 1;
});
const remote = usePagedRemote<Page<Delivery>>(
  () =>
    platform.currentId
      ? `/platforms/${platform.currentId}/notifications?page=${page.value}&page_size=${size.value}`
      : "",
  () => platform.currentId
);
const kinds: Record<string, string> = {
  channel: "渠道状态",
  balance: "用户余额",
  request_error: "请求错误",
  daily: "日报",
  weekly: "周报",
  service: "检查失败"
};
const statuses: Record<string, string> = {
  sent: "已投递",
  pending: "等待投递",
  sending: "投递中",
  historical: "历史停发（待确认）",
  dead: "投递失败"
};
const columns = [
  {
    label: "通知",
    minWidth: 140,
    formatter: (r: Delivery) => `${kinds[r.kind] ?? r.kind} #${r.id}`
  },
  { label: "投递状态", slot: "status", minWidth: 180 },
  { label: "尝试次数", prop: "attempts", width: 100 },
  {
    label: "生成时间",
    minWidth: 200,
    formatter: (r: Delivery) => timeCN(r.created_at, platform.current?.timezone)
  },
  { label: "操作", slot: "action", width: 100, fixed: "right" }
];
async function retry(item: Delivery): Promise<void> {
  retrying.value = item.id;
  await perform(async () => {
    await request(
      `/platforms/${platform.currentId}/notification-retries`,
      "POST",
      { notification_id: item.id, request_id: crypto.randomUUID() }
    );
    await remote.refresh();
  }, "已重新排队；成功投递的目标不会重复发送");
  retrying.value = null;
}
</script>
<template>
  <ConsolePage
    title="通知投递"
    description="查看正式告警与报告的投递状态，失败通知可手动重新排队"
    @refresh="remote.refresh"
    ><el-alert
      v-if="remote.error.value"
      :title="remote.error.value"
      type="error"
      :closable="false"
    />
    <section class="surface">
      <pure-table
        :data="remote.data.value?.items ?? []"
        :columns="columns"
        :loading="remote.loading.value"
        row-key="id"
        ><template #status="{ row }"
          ><StatusDot
            :text="
              row.delivery_method === 'manual'
                ? '已投递（人工标记）'
                : (statuses[row.status] ?? row.status)
            "
            :tone="
              row.status === 'sent'
                ? 'ok'
                : row.status === 'dead'
                  ? 'warn'
                  : 'muted'
            "
          /><small
            v-if="row.last_error"
            :class="
              row.delivery_method === 'manual'
                ? 'section-footnote'
                : 'delivery-error'
            "
            >{{ row.last_error }}</small
          ></template
        ><template #action="{ row }"
          ><el-popconfirm
            v-if="['dead', 'historical'].includes(row.status)"
            title="重试此历史通知？仍按该通知原有目标投递"
            @confirm="retry(row)"
            ><template #reference
              ><el-button link type="primary" :loading="retrying === row.id"
                >重试</el-button
              ></template
            ></el-popconfirm
          ><span v-else class="muted">—</span></template
        ></pure-table
      ><el-pagination
        v-model:current-page="page"
        v-model:page-size="size"
        :total="remote.total.value"
        :page-sizes="[20, 50, 100]"
        layout="total,sizes,prev,pager,next,jumper"
      />
      <p class="section-footnote">
        无匹配且已启用的 Webhook
        不会显示“已投递”；通知按平台和事件订阅隔离，不对未授权目标转发
      </p>
    </section></ConsolePage
  >
</template>
<style scoped>
.delivery-error {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--danger);
}
</style>
