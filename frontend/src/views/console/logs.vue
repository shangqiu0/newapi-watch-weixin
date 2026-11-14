<script setup lang="ts">
import { ref, watch } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { usePagedRemote } from "@/composables/usePagedRemote";
import type { LogRecord, Page } from "@/api/types";
import { number, quotaText, timeCN } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
const store = usePlatformStore(),
  page = ref(1),
  size = ref(20),
  period = ref("今天"),
  type = ref(0),
  httpStatus = ref<number | undefined>(),
  cursor = ref(""),
  selected = ref<LogRecord | null>(null),
  drawer = ref(false);
watch([() => store.currentId, period, type, httpStatus, size], () => {
  page.value = 1;
  cursor.value = "";
});
const { data, loading, error, refresh, total } = usePagedRemote<
  Page<LogRecord> & { cursor?: string; has_more?: boolean; limitation?: string }
>(
  () =>
    store.currentId
      ? `/platforms/${store.currentId}/logs?period=${encodeURIComponent(period.value)}&page=${page.value}&page_size=${size.value}&log_type=${type.value}&http_status=${httpStatus.value ?? 0}&cursor=${cursor.value}`
      : "",
  () => store.currentId
);
const columns = [
  {
    label: "时间",
    minWidth: 195,
    formatter: (r: LogRecord) => timeCN(r.created_at, store.current?.timezone)
  },
  {
    label: "渠道",
    minWidth: 150,
    formatter: (r: LogRecord) => `${r.channel_name || "渠道"} #${r.channel_id}`
  },
  { label: "模型", prop: "model_name", minWidth: 170 },
  {
    label: "类型",
    width: 95,
    formatter: (r: LogRecord) =>
      r.type === 5 ? "错误" : r.type === 2 ? "消费" : String(r.type)
  },
  { label: "HTTP", width: 80, prop: "http_status", slot: "http" },
  {
    label: "消耗金额（¥）",
    align: "right",
    minWidth: 130,
    formatter: (r: LogRecord) => quotaText(r.quota, r.quota_display)
  },
  { label: "错误码", prop: "error_code", minWidth: 180 },
  { label: "详情", fixed: "right", width: 80, slot: "action" }
];
</script>
<template>
  <ConsolePage
    title="请求日志"
    description="向绑定平台按页查询；HTTP状态码采用有界错误日志扫描，可继续读取，不伪造全量总数"
    @refresh="refresh"
    ><template #actions
      ><el-button :loading="loading" @click="refresh"
        >重新查询</el-button
      ></template
    >
    <section class="surface">
      <div class="table-toolbar">
        <el-select v-model="period" style="width: 125px" aria-label="日志周期"
          ><el-option
            v-for="p in ['今天', '昨天', '近7天', '近30天']"
            :key="p"
            :value="p"
            :label="p" /></el-select
        ><el-select v-model="type" style="width: 125px" aria-label="日志类型"
          ><el-option label="全部记录" :value="0" /><el-option
            label="消费记录"
            :value="2" /><el-option label="错误记录" :value="5" /></el-select
        ><el-input-number
          v-model="httpStatus"
          :min="400"
          :max="599"
          placeholder="HTTP 状态码"
          aria-label="HTTP状态码"
          :controls="false"
        /><span class="toolbar-meta">{{
          httpStatus
            ? `本批 ${number(data?.items.length)} 条匹配 · 总数未统计`
            : `${number(data?.total)} 条 · 平台管理 API`
        }}</span>
      </div>
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        :closable="false"
        description="未取到数据不代表平台没有请求"
      /><pure-table
        :data="data?.items ?? []"
        :columns="columns"
        :loading="loading"
        row-key="id"
        ><template #http="{ row }"
          ><el-tag v-if="row.http_status" type="danger" effect="plain">{{
            row.http_status
          }}</el-tag
          ><span v-else>—</span></template
        ><template #action="{ row }"
          ><el-button
            type="primary"
            link
            @click="
              selected = row;
              drawer = true;
            "
            >详情</el-button
          ></template
        ></pure-table
      >
      <div v-if="httpStatus" class="table-toolbar">
        <span>{{ data?.limitation }}</span
        ><el-button
          :disabled="!data?.has_more || loading"
          @click="cursor = data?.cursor ?? ''"
          >继续查找</el-button
        ><el-button
          :disabled="loading"
          @click="
            cursor = '';
            refresh();
          "
          >从头查询</el-button
        >
      </div>
      <el-pagination
        v-else
        v-model:current-page="page"
        v-model:page-size="size"
        :page-sizes="[20, 50, 100]"
        :total="total"
        layout="total,sizes,prev,pager,next,jumper"
      />
    </section>
    <el-drawer v-model="drawer" title="日志明细" size="520px"
      ><el-descriptions v-if="selected" :column="1" border
        ><el-descriptions-item label="渠道"
          >{{ selected.channel_name }} #{{
            selected.channel_id
          }}</el-descriptions-item
        ><el-descriptions-item label="模型">{{
          selected.model_name
        }}</el-descriptions-item
        ><el-descriptions-item label="HTTP 状态">{{
          selected.http_status || "平台未提供"
        }}</el-descriptions-item
        ><el-descriptions-item label="错误码">{{
          selected.error_code || "未提供"
        }}</el-descriptions-item
        ><el-descriptions-item label="错误信息">
          <pre class="error-detail">{{
            selected.error_message || "无可见错误信息"
          }}</pre></el-descriptions-item
        ><el-descriptions-item label="Token"
          >{{ number(selected.prompt_tokens) }} 输入 /
          {{ number(selected.completion_tokens) }} 输出</el-descriptions-item
        ></el-descriptions
      ></el-drawer
    ></ConsolePage
  >
</template>
