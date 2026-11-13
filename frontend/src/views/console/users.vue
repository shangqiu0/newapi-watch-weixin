<script setup lang="ts">
import { ref, watch } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { usePagedRemote } from "@/composables/usePagedRemote";
import type { Page, UserInfo } from "@/api/types";
import { number } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import { useRouter } from "vue-router";
const store = usePlatformStore(),
  router = useRouter(),
  keyword = ref(""),
  term = ref(""),
  page = ref(1),
  size = ref(20);
watch([() => store.currentId, term, size], () => {
  page.value = 1;
});
const { data, loading, error, refresh, total } = usePagedRemote<Page<UserInfo>>(
  () =>
    store.currentId
      ? `/platforms/${store.currentId}/users?keyword=${encodeURIComponent(term.value)}&page=${page.value}&page_size=${size.value}`
      : "",
  () => store.currentId
);
const columns = [
  { label: "用户ID", prop: "id", width: 95 },
  { label: "用户", prop: "username", minWidth: 160 },
  { label: "显示名", prop: "display_name", minWidth: 140 },
  {
    label: "余额 quota",
    align: "right",
    minWidth: 150,
    formatter: (r: UserInfo) => number(r.quota)
  },
  {
    label: "已用 quota",
    align: "right",
    minWidth: 150,
    formatter: (r: UserInfo) => number(r.used_quota)
  },
  {
    label: "请求数",
    align: "right",
    minWidth: 110,
    formatter: (r: UserInfo) => number(r.request_count)
  },
  { label: "分组", prop: "group", minWidth: 120 },
  { label: "监控", slot: "action", width: 100 }
];
function search() {
  page.value = 1;
  if (term.value === keyword.value) refresh();
  else term.value = keyword.value;
}
</script>
<template>
  <ConsolePage
    title="用户额度"
    description="使用当前平台管理令牌查询用户，余额以平台原始 quota 单位显示"
    @refresh="refresh"
    ><section class="surface">
      <div class="table-toolbar">
        <el-input
          v-model="keyword"
          placeholder="输入用户名搜索"
          aria-label="搜索用户"
          style="max-width: 300px"
          clearable
          @keyup.enter="search"
        /><el-button type="primary" :loading="loading" @click="search"
          >查询用户</el-button
        ><span class="toolbar-meta"
          >{{ number(data?.total) }} 个用户 · 平台管理 API</span
        >
      </div>
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
        ><template #action="{ row }"
          ><el-button
            type="primary"
            link
            @click="
              router.push({
                path: '/console/balances',
                query: { user: row.id, name: row.username }
              })
            "
            >设置阈值</el-button
          ></template
        ></pure-table
      ><el-pagination
        v-model:current-page="page"
        v-model:page-size="size"
        :page-sizes="[20, 50, 100]"
        :total="total"
        layout="total,sizes,prev,pager,next,jumper"
      />
      <p class="section-footnote">
        不展示用户令牌、IP 等敏感数据。不同用户可设置不同余额阈值
      </p>
    </section></ConsolePage
  >
</template>
