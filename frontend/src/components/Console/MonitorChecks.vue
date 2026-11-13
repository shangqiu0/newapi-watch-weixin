<script setup lang="ts">
import { ElTable, ElTableColumn, ElEmpty } from "element-plus";
import type { MonitorCheck } from "@/api/types";
import { timeCN, number } from "@/utils/format";
import StatusDot from "./StatusDot.vue";
withDefaults(defineProps<{ checks: MonitorCheck[]; timezone?: string }>(), {
  timezone: "Asia/Shanghai"
});
function label(check: MonitorCheck): string {
  if (!check.enabled) return "已停用";
  if (check.checking) return "正在检查";
  const stage = check.detail.report_stage;
  if (stage === "collecting") return "报告汇总中";
  if (stage === "paused") return "本期已暂停";
  if (stage === "failed") return "报告生成失败";
  if (stage === "queued") return "报告已入队";
  if (stage === "scheduled") return "等待计划时间";
  if (check.stale) return "检查已逾期";
  return {
    ok: "检查成功",
    unknown: "检查失败 · 状态未知",
    pending: "等待首次检查",
    disabled: "已停用"
  }[check.status];
}
function tone(check: MonitorCheck): "ok" | "warn" | "muted" {
  return !check.enabled || check.checking || check.status === "pending"
    ? "muted"
    : check.status === "unknown" || check.stale
      ? "warn"
      : "ok";
}
</script>
<template>
  <el-empty
    v-if="!checks.length"
    description="尚无检查对象，请先配置监控策略"
  />
  <el-table v-else :data="checks" row-key="id" class="monitor-checks">
    <el-table-column label="监控检查" min-width="160"
      ><template #default="{ row }"
        ><span class="check-name">{{ row.name }}</span
        ><small class="check-caption">{{
          row.kind === "daily" || row.kind === "weekly"
            ? "独立报告调度"
            : `每 ${number(row.interval)} 秒`
        }}</small></template
      ></el-table-column
    >
    <el-table-column label="执行状态" min-width="200"
      ><template #default="{ row }"
        ><StatusDot
          :text="label(row as MonitorCheck)"
          :tone="tone(row as MonitorCheck)"
        /><small
          v-if="row.detail.report_stage === 'collecting'"
          class="check-caption"
          >已读取 {{ number(row.detail.records_fetched) }} /
          {{
            row.detail.total_records === null
              ? "未知总数"
              : number(row.detail.total_records)
          }}
          条，已完成页不会重读</small
        ><small
          v-if="row.error && row.enabled"
          class="check-caption error-text"
          >{{ row.error }}</small
        ><small v-else-if="row.detail.lag_seconds" class="check-caption"
          >日志窗口追赶中 · 落后 {{ number(row.detail.lag_seconds) }} 秒</small
        ><small v-else-if="row.detail.note" class="check-caption">{{
          row.detail.note
        }}</small></template
      ></el-table-column
    >
    <el-table-column label="最近成功" min-width="188"
      ><template #default="{ row }"
        ><span class="check-time">{{
          timeCN(row.last_ok_at, timezone)
        }}</span></template
      ></el-table-column
    >
    <el-table-column label="下一次检查" min-width="188"
      ><template #default="{ row }"
        ><span class="check-time">{{
          row.enabled
            ? row.next_at
              ? timeCN(row.next_at, timezone)
              : "即将执行"
            : "不执行"
        }}</span></template
      ></el-table-column
    >
  </el-table>
</template>
<style scoped>
.check-name {
  font-weight: 500;
}

.check-caption {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--muted);
  overflow-wrap: anywhere;
}

.check-time {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.error-text {
  color: var(--danger);
}
</style>
