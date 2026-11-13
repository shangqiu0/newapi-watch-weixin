<script setup lang="ts">
defineProps<{ text: string; tone?: "ok" | "warn" | "danger" | "muted" }>();
</script>
<template>
  <span class="status-dot-label"
    ><i :class="tone ?? 'ok'" aria-hidden="true" />{{ text }}</span
  >
</template>
