<script setup lang="ts">
import { computed, ref, watch, nextTick } from "vue";
import { Send, Database } from "lucide-vue-next";
import { usePlatformStore } from "@/store/modules/platform";
import { useChatStore } from "@/store/modules/chat";
import { request } from "@/api/console";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import StatusDot from "@/components/Console/StatusDot.vue";
const store = usePlatformStore(),
  chat = useChatStore();
const conversation = computed(() => chat.conversation(store.currentId));
const messages = ref<HTMLElement>();
const hints = [
  "现在有多少渠道？分别是什么状态？",
  "最近7天渠道用量排行，说明数据来源",
  "今天有哪些400或500错误？",
  "查询用户额度与用量"
];
async function scroll() {
  await nextTick();
  if (messages.value) messages.value.scrollTop = messages.value.scrollHeight;
}
watch(() => conversation.value.entries.length, scroll);
watch(() => store.currentId, scroll);
async function send() {
  const id = store.currentId,
    c = chat.conversation(id),
    text = c.draft.trim(),
    epoch = chat.epoch;
  if (!text || c.pending || !store.current?.enabled) return;
  c.pending = true;
  c.draft = "";
  chat.append(id, { role: "user", text });
  try {
    const result = await request<{
      text: string;
      ok: boolean;
      image_base64?: string;
    }>(`/platforms/${id}/queries`, "POST", { text });
    if (epoch === chat.epoch && chat.conversations[id] === c)
      chat.append(id, {
        role: "assistant",
        text: result.text,
        image: result.image_base64,
        error: !result.ok
      });
  } catch (e) {
    if (epoch === chat.epoch && chat.conversations[id] === c)
      chat.append(id, {
        role: "assistant",
        text: e instanceof Error ? e.message : "查询失败",
        error: true
      });
  } finally {
    c.pending = false;
    scroll();
  }
}
function key(event: KeyboardEvent) {
  if (
    event.key === "Enter" &&
    (event.ctrlKey || event.metaKey) &&
    !event.isComposing
  ) {
    event.preventDefault();
    void send();
  }
}
</script>
<template>
  <ConsolePage
    title="问答工作区"
    description="机器人与管理台共用当前平台数据工具；实时来源和覆盖范围由结果明确说明"
    ><div class="chat-grid">
      <section class="surface chat-surface">
        <div class="chat-header">
          <span>{{ store.current?.name }}</span
          ><StatusDot
            :text="store.current?.llm.enabled ? '模型工具问答' : '直接命令查询'"
          />
        </div>
        <div
          ref="messages"
          class="chat-messages"
          role="log"
          aria-label="当前平台对话"
        >
          <el-empty
            v-if="!conversation.entries.length"
            description="从一个具体的运维问题开始，不跨平台取数"
          />
          <article
            v-for="(item, index) in conversation.entries"
            :key="index"
            :class="['chat-entry', item.role, { failed: item.error }]"
          >
            <label>{{
              item.role === "user" ? "你" : `${store.current?.name} · 运维助手`
            }}</label>
            <div class="chat-bubble">
              {{ item.text
              }}<img
                v-if="item.image"
                :src="`data:image/png;base64,${item.image}`"
                alt="当前平台统计图表"
              />
            </div>
          </article>
          <div v-if="conversation.pending" class="query-pending">
            <el-skeleton :rows="2" /><span>正在读取平台数据并整理回答…</span>
          </div>
        </div>
        <form class="chat-input" @submit.prevent="send">
          <el-input
            v-model="conversation.draft"
            type="textarea"
            :rows="2"
            maxlength="2000"
            placeholder="例如：查看全部渠道状态和最近7天用量，说明异常是否需要处理"
            aria-label="向当前平台提问"
            :disabled="!store.current?.enabled"
            @keydown="key"
          /><el-button
            type="primary"
            native-type="submit"
            :loading="conversation.pending"
            :disabled="!store.current?.enabled"
            ><Send :size="16" />发送</el-button
          >
        </form>
        <div class="chat-input-hint">
          Ctrl / ⌘ + Enter 发送 · 对话按平台隔离，仅保留本次登录的最近 100 条
        </div>
      </section>
      <aside class="surface chat-inspector">
        <div class="section-heading">
          <h2>当前数据范围</h2>
          <Database :size="18" />
        </div>
        <p>{{ store.current?.name }}</p>
        <div class="section-footnote">
          渠道与统计优先通过此平台管理接口读取；不能访问其他平台、任意 SQL
          或外部地址。上游限流、权限不足和数据缺失会明确报告
        </div>
        <h3>快捷提问</h3>
        <el-button
          v-for="hint in hints"
          :key="hint"
          class="query-hint"
          @click="conversation.draft = hint"
          >{{ hint }}</el-button
        >
      </aside>
    </div></ConsolePage
  >
</template>
