<script setup lang="ts">
import { ref, watch } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { usePagedRemote } from "@/composables/usePagedRemote";
import type { Channel, Page } from "@/api/types";
import { number, statusNames, timeCN } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import StatusDot from "@/components/Console/StatusDot.vue";
const store = usePlatformStore(),
  page = ref(1),
  size = ref(20),
  keyword = ref(""),
  status = ref<number | null>(null),
  live = ref(true),
  selected = ref<Channel | null>(null),
  drawer = ref(false);
watch(
  [() => store.currentId, keyword, status, live, size],
  () => (page.value = 1)
);
const { data, loading, error, refresh, total } = usePagedRemote<Page<Channel>>(
  () =>
    store.currentId
      ? `/platforms/${store.currentId}/channels?page=${page.value}&page_size=${size.value}&keyword=${encodeURIComponent(keyword.value)}&live=${live.value}${status.value === null ? "" : `&status=${status.value}`}`
      : "",
  () => store.currentId
);
const columns = [
  { label: "渠道", prop: "name", minWidth: 220, slot: "name" },
  { label: "状态", prop: "status", minWidth: 140, slot: "status" },
  {
    label: "余额（原始值）",
    align: "right",
    minWidth: 150,
    formatter: (r: Channel) =>
      r.balance === null ? "未返回" : number(r.balance)
  },
  {
    label: "响应时间",
    align: "right",
    minWidth: 130,
    formatter: (r: Channel) => `${number(r.response_time)} ms`
  },
  { label: "操作", width: 100, slot: "action", fixed: "right" }
];
function details(row: Channel) {
  selected.value = row;
  drawer.value = true;
}
</script>
<template>
  <ConsolePage
    title="渠道状态"
    description="全部渠道分页读取，不再限制前 50 条；手动禁用不等于故障"
    @refresh="refresh"
    ><template #actions
      ><el-switch
        v-model="live"
        active-text="实时查询"
        inactive-text="快照"
      /><el-button :loading="loading" @click="refresh"
        >查询渠道</el-button
      ></template
    >
    <section class="surface">
      <div class="table-toolbar">
        <el-input
          v-model="keyword"
          clearable
          placeholder="搜索渠道名称或 ID"
          aria-label="搜索渠道"
          style="max-width: 280px"
        /><el-select
          v-model="status"
          clearable
          placeholder="全部状态"
          style="width: 160px"
          ><el-option
            v-for="(label, value) in statusNames"
            :key="value"
            :value="Number(value)"
            :label="label" /></el-select
        ><span class="toolbar-meta"
          >全部 {{ number(data?.all_total ?? data?.total) }} 个 ·
          {{ live ? "平台管理 API" : "本地快照" }}</span
        >
      </div>
      <el-alert
        v-if="error"
        type="error"
        :title="error"
        :closable="false"
      /><pure-table
        :data="data?.items ?? []"
        :columns="columns"
        :loading="loading"
        row-key="id"
        ><template #empty
          ><el-empty description="没有符合条件的渠道"
            ><el-button
              @click="
                keyword = '';
                status = null;
                refresh();
              "
              >重置筛选并查询</el-button
            ></el-empty
          ></template
        ><template #name="{ row }"
          ><span>{{ row.name }}</span
          ><span class="cell-id">#{{ row.id }}</span></template
        ><template #status="{ row }"
          ><StatusDot
            :text="statusNames[row.status] ?? '未知'"
            :tone="
              row.status === 1 ? 'ok' : row.status === 3 ? 'danger' : 'muted'
            " /></template
        ><template #action="{ row }"
          ><el-button link type="primary" @click="details(row)"
            >详情</el-button
          ></template
        ></pure-table
      ><el-pagination
        v-model:current-page="page"
        v-model:page-size="size"
        :page-sizes="[20, 50, 100]"
        :total="total"
        layout="total,sizes,prev,pager,next,jumper"
      />
    </section>
    <el-drawer v-model="drawer" title="渠道明细" size="460px"
      ><el-descriptions v-if="selected" :column="1" border
        ><el-descriptions-item label="渠道"
          >{{ selected.name }} #{{ selected.id }}</el-descriptions-item
        ><el-descriptions-item label="状态">{{
          statusNames[selected.status]
        }}</el-descriptions-item
        ><el-descriptions-item label="余额">{{
          selected.balance === null ? "平台未返回" : number(selected.balance)
        }}</el-descriptions-item
        ><el-descriptions-item label="响应时间"
          >{{ selected.response_time }} ms</el-descriptions-item
        ><el-descriptions-item label="分组">{{
          selected.group || "未提供"
        }}</el-descriptions-item>
        <el-descriptions-item label="优先级 / 权重"
          >{{ selected.priority ?? "—" }} /
          {{ selected.weight ?? "—" }}</el-descriptions-item
        >
        <el-descriptions-item label="累计消耗 quota">{{
          number(selected.used_quota)
        }}</el-descriptions-item>
        <el-descriptions-item label="最近测试">{{
          timeCN(selected.test_time || null, store.current?.timezone)
        }}</el-descriptions-item>
        <el-descriptions-item label="余额更新">{{
          timeCN(selected.balance_updated_time || null, store.current?.timezone)
        }}</el-descriptions-item>
        <el-descriptions-item label="模型">{{
          selected.models || "平台未提供"
        }}</el-descriptions-item></el-descriptions
      >
      <p class="section-footnote">
        该查询只读，不会探测、启停或修改渠道
      </p></el-drawer
    ></ConsolePage
  >
</template>
