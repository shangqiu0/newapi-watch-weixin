<script setup lang="ts">
import { ref, watch } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { usePagedRemote } from "@/composables/usePagedRemote";
import { usePageBounds } from "@/composables/usePageBounds";
import { timeCN } from "@/utils/format";
import CommandRecords from "./CommandRecords.vue";
import type { Page } from "@/api/types";
interface Operation {
  id: string;
  action: string;
  object_id: string;
  outcome: string;
  created_at: number;
}
const store = usePlatformStore(),
  page = ref(1),
  size = ref(20),
  tab = ref("operations");
const remote = usePagedRemote<Page<Operation>>(
  () =>
    store.currentId
      ? `/platforms/${store.currentId}/operation-records?page=${page.value}&page_size=${size.value}`
      : "",
  () => store.currentId
);
watch([() => store.currentId, size], () => {
  page.value = 1;
});
usePageBounds(remote.total, page, size);
const actions: Record<string, string> = {
  PATCH: "更新配置",
  PUT: "保存策略",
  DELETE: "删除或取消",
  POST: "执行操作"
};
const resources: Record<string, string> = {
  bindings: "机器人与通知",
  monitoring: "监控策略",
  cleanups: "本地数据清理",
  "notification-retries": "通知重试",
  "notification-cancellations": "取消投递",
  "upstream-accounts": "上游账户",
  "query-tasks": "查询任务"
};
function label(value: string): string {
  const [method, path] = value.split(" ");
  const resource = Object.keys(resources).find(key =>
    path?.includes("/" + key)
  );
  return `${actions[method] ?? method} · ${resource ? resources[resource] : "平台设置"}`;
}
const columns = [
  {
    label: "时间",
    minWidth: 200,
    formatter: (r: Operation) => timeCN(r.created_at, store.current?.timezone)
  },
  {
    label: "操作",
    minWidth: 200,
    formatter: (r: Operation) => label(r.action)
  },
  { label: "对象标识", prop: "object_id", minWidth: 200 },
  { label: "结果", formatter: () => "成功", width: 100 }
];
</script>
<template>
  <section class="surface">
    <div class="section-heading">
      <div>
        <h2>运行记录</h2>
        <p>必要的配置与数据维护操作可追溯，不记录密钥和原始问题。</p>
      </div>
    </div>
    <el-tabs v-model="tab"
      ><el-tab-pane label="管理操作" name="operations"
        ><el-alert
          v-if="remote.error.value"
          :title="remote.error.value"
          type="error"
          :closable="false" /><pure-table
          :data="remote.data.value?.items ?? []"
          :columns="columns"
          :loading="remote.loading.value"
          row-key="id"
          empty-text="暂无管理操作记录" /><el-pagination
          v-model:current-page="page"
          v-model:page-size="size"
          :page-sizes="[20, 50, 100]"
          :total="remote.total.value"
          layout="total,sizes,prev,pager,next" /></el-tab-pane
      ><el-tab-pane label="命令调用" name="commands" lazy
        ><CommandRecords v-if="tab === 'commands'" /></el-tab-pane
    ></el-tabs>
  </section>
</template>
