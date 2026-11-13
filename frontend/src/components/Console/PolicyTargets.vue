<script setup lang="ts">
import { computed } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
const props = defineProps<{ modelValue: string[]; kind: string }>();
const emit = defineEmits<{ "update:modelValue": [value: string[]] }>();
const platform = usePlatformStore();
const targets = computed(
  () =>
    platform.overview?.bindings.filter(
      b => b.kind === "webhook" && b.events.includes(props.kind)
    ) ?? []
);
</script>
<template>
  <el-select
    :model-value="modelValue"
    multiple
    clearable
    placeholder="全部订阅此事件的本平台 Webhook"
    style="width: 100%"
    @update:model-value="value => emit('update:modelValue', value)"
    ><el-option
      v-for="target in targets"
      :key="target.id"
      :value="target.id"
      :label="target.name + (target.enabled ? '' : '（已停用）')"
  /></el-select>
  <p class="section-footnote">
    留空使用本平台订阅目标；{{
      targets.length
        ? "停用的目标不会投递"
        : "尚无订阅目标，请到「机器人与通知」配置"
    }}
  </p>
</template>
