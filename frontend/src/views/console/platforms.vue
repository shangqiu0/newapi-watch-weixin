<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { request, perform } from "@/api/console";
import type { Platform } from "@/api/types";
import { timeCN } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import PlatformEditor from "@/components/Console/PlatformEditor.vue";
import PlatformDeleteDialog from "@/components/Console/PlatformDeleteDialog.vue";
import StatusDot from "@/components/Console/StatusDot.vue";
const store = usePlatformStore();
const editorOpen = ref(false),
  deleting = ref(false),
  editing = ref<Platform>(),
  removal = ref<Platform | null>(null);
const page = ref(1),
  size = ref(20),
  keyword = ref(""),
  refreshing = ref(false),
  toggling = ref<string | null>(null);
const filtered = computed(() =>
  store.list.filter(p =>
    `${p.name} ${p.newapi_url}`
      .toLowerCase()
      .includes(keyword.value.trim().toLowerCase())
  )
);
const rows = computed(() =>
  filtered.value.slice((page.value - 1) * size.value, page.value * size.value)
);
watch([keyword, size], () => {
  page.value = 1;
});
watch(
  () => filtered.value.length,
  count => {
    page.value = Math.min(
      page.value,
      Math.max(1, Math.ceil(count / size.value))
    );
  }
);
const columns = [
  { label: "监控平台", slot: "name", minWidth: 200 },
  { label: "NewAPI 地址", prop: "newapi_url", minWidth: 240 },
  { label: "状态", slot: "status", minWidth: 120 },
  { label: "管理用户 ID", prop: "newapi_user_id", width: 120 },
  {
    label: "更新时间",
    minWidth: 188,
    formatter: (p: Platform) => timeCN(p.updated_at, p.timezone)
  },
  { label: "操作", slot: "actions", fixed: "right", width: 224 }
];
function edit(platform?: Platform): void {
  editing.value = platform ? { ...platform } : undefined;
  editorOpen.value = true;
}
function confirmRemoval(platform: Platform): void {
  removal.value = { ...platform };
  deleting.value = true;
}
async function refresh(): Promise<void> {
  if (refreshing.value) return;
  refreshing.value = true;
  await perform(async () => {
    await store.load();
  });
  refreshing.value = false;
}
async function toggle(platform: Platform): Promise<void> {
  if (toggling.value) return;
  toggling.value = platform.id;
  await perform(
    async () => {
      await request(`/platforms/${platform.id}`, "PATCH", {
        enabled: !platform.enabled
      });
      await store.load();
    },
    platform.enabled ? "平台已停用" : "平台已启用"
  );
  toggling.value = null;
}
</script>
<template>
  <ConsolePage
    title="平台管理"
    description="管理接入平台、连接凭据及启停，平台之间独立运行"
    platform-optional
    @refresh="refresh"
    ><template #actions
      ><el-button type="primary" @click="edit()">新增平台</el-button></template
    >
    <section class="surface">
      <div class="table-toolbar">
        <el-input
          v-model="keyword"
          placeholder="搜索平台名称或地址"
          aria-label="搜索监控平台"
          clearable
          class="platform-search"
        /><span class="toolbar-meta">{{ store.list.length }} 个接入平台</span>
      </div>
      <el-skeleton v-if="refreshing && !store.list.length" :rows="5" />
      <el-empty
        v-else-if="!store.list.length"
        description="还没有接入的监控平台"
        ><el-button type="primary" @click="edit()"
          >接入第一个平台</el-button
        ></el-empty
      >
      <pure-table
        v-else
        :data="rows"
        :columns="columns"
        :loading="refreshing"
        row-key="id"
        ><template #name="{ row }"
          ><div class="platform-name">
            <span>{{ row.name }}</span
            ><span
              v-if="row.id === store.currentId"
              class="current-platform-label"
              >当前平台</span
            >
          </div></template
        >
        <template #status="{ row }"
          ><StatusDot
            :text="
              row.runtime_status === 'error'
                ? '启动异常'
                : row.enabled
                  ? '已启用'
                  : '已停用'
            "
            :tone="
              row.runtime_status === 'error'
                ? 'warn'
                : row.enabled
                  ? 'ok'
                  : 'muted'
            "
        /></template>
        <template #actions="{ row }"
          ><el-button link type="primary" @click="edit(row)">编辑</el-button
          ><el-popconfirm
            :title="
              row.enabled
                ? '暂停此平台的监控与机器人连接？'
                : '启用此平台的监控与机器人连接？'
            "
            @confirm="toggle(row)"
            ><template #reference
              ><el-button
                link
                :loading="toggling === row.id"
                :disabled="!!toggling"
                >{{ row.enabled ? "停用" : "启用" }}</el-button
              ></template
            ></el-popconfirm
          ><el-button link type="danger" @click="confirmRemoval(row)"
            >删除</el-button
          ></template
        >
      </pure-table>
      <el-pagination
        v-if="store.list.length"
        v-model:current-page="page"
        v-model:page-size="size"
        :page-sizes="[20, 50, 100]"
        :total="filtered.length"
        layout="total,sizes,prev,pager,next,jumper"
      />
      <p class="section-footnote">
        停用保留列表入口，随时可重新启用；删除会停止连接并移出列表，本地历史保留，不影响上游站点
      </p>
    </section>
    <PlatformEditor
      v-model="editorOpen"
      :platform="editing"
    /><PlatformDeleteDialog v-model="deleting" :platform="removal" />
  </ConsolePage>
</template>
<style scoped>
.platform-search {
  max-width: 320px;
}

.platform-name {
  display: flex;
  gap: 12px;
  align-items: center;
}

.current-platform-label {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--muted);
}
</style>
