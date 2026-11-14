<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount } from "vue";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { Statistics } from "@/api/types";
import { shortDate, timeCN, number } from "@/utils/format";
echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);
const props = defineProps<{ data: Statistics }>();
const root = ref<HTMLElement>();
let chart: echarts.ECharts | null = null;
let resize: ResizeObserver | null = null;
let theme: MutationObserver | null = null;
function draw() {
  if (!root.value) return;
  chart?.dispose();
  chart = echarts.init(root.value);
  const style = getComputedStyle(document.documentElement);
  const accent = style.getPropertyValue("--accent").trim();
  const points = props.data.series.filter(
    p => !p.label.includes("T") || new Date(p.label).getTime() <= Date.now()
  );
  chart.setOption({
    animation: !matchMedia("(prefers-reduced-motion: reduce)").matches,
    grid: { left: 64, right: 16, top: 24, bottom: 40 },
    tooltip: {
      trigger: "axis",
      renderMode: "richText",
      formatter: (params: unknown) => {
        const p = (params as { dataIndex: number }[])[0];
        const item = points[p.dataIndex];
        return `${timeCN(item.label, props.data.timezone)}\n消耗 ${number(item.quota)} quota\n消费 ${number(item.success_records)} · 错误 ${props.data.unavailable_metrics?.includes("error_records") ? "未提供" : number(item.error_records)}`;
      }
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: points.map(p => p.label),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: style.getPropertyValue("--muted"),
        fontFamily: "Consolas",
        formatter: (v: string) =>
          v.includes("T") ? `${shortDate(v)} ${v.slice(11, 16)}` : shortDate(v)
      }
    },
    yAxis: {
      type: "value",
      axisLabel: {
        color: style.getPropertyValue("--muted"),
        fontFamily: "Consolas"
      },
      splitLine: {
        lineStyle: { color: style.getPropertyValue("--line"), type: "dashed" }
      }
    },
    series: [
      {
        type: "line",
        data: points.map(p => p.quota),
        symbolSize: 5,
        showSymbol: false,
        lineStyle: { color: accent, width: 2 },
        itemStyle: { color: accent },
        areaStyle: { color: accent, opacity: 0.12 }
      }
    ]
  });
}
watch(() => props.data, draw);
onMounted(() => {
  draw();
  resize = new ResizeObserver(() => chart?.resize());
  resize.observe(root.value);
  theme = new MutationObserver(draw);
  theme.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class"]
  });
});
onBeforeUnmount(() => {
  resize?.disconnect();
  theme?.disconnect();
  chart?.dispose();
});
</script>
<template>
  <div
    ref="root"
    class="trend-chart"
    role="img"
    aria-label="当前平台消耗趋势图"
  />
</template>
