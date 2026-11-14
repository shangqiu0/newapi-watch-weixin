<script setup lang="ts">
import { ref, reactive, computed } from "vue";
import { Bot, Bell, Plus } from "lucide-vue-next";
import { ElMessageBox } from "element-plus";
import { usePlatformStore } from "@/store/modules/platform";
import { request, perform } from "@/api/console";
import type { Binding } from "@/api/types";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import StatusDot from "@/components/Console/StatusDot.vue";
const store = usePlatformStore(),
  dialog = ref(false),
  existing = ref<Binding | null>(null),
  busy = ref(false);
const form = reactive({
  name: "",
  kind: "bot" as "bot" | "webhook",
  enabled: false,
  bot_id: "",
  bot_secret: "",
  notification_chat_id: "",
  webhook_url: "",
  events: [] as string[],
  message_format: "markdown" as "markdown" | "text",
  mention_all: false
});
const events = [
  ["channel", "渠道状态"],
  ["request_error", "HTTP 错误"],
  ["balance", "账户余额"],
  ["daily", "日报"],
  ["weekly", "周报"],
  ["service", "服务异常"]
];
const grouped = computed(() =>
  ["bot", "webhook"].map(kind => ({
    kind,
    items: (store.overview?.bindings ?? []).filter(b => b.kind === kind)
  }))
);
function edit(binding?: Binding) {
  existing.value = binding ?? null;
  Object.assign(
    form,
    {
      name: "",
      kind: "bot",
      enabled: false,
      bot_id: "",
      bot_secret: "",
      notification_chat_id: "",
      webhook_url: "",
      events: events.map(e => e[0]),
      message_format: "markdown",
      mention_all: false
    },
    binding
      ? {
          ...binding,
          bot_id: "",
          bot_secret: "",
          notification_chat_id: binding.notification_chat_id ?? "",
          webhook_url: "",
          events: [...binding.events]
        }
      : {}
  );
  dialog.value = true;
}
async function save() {
  busy.value = true;
  const id = store.currentId;
  await perform(async () => {
    const body = {
      name: form.name,
      enabled: form.enabled,
      ...(!existing.value ? { kind: form.kind } : {}),
      ...(form.kind === "bot"
        ? {
            bot_id: form.bot_id,
            bot_secret: form.bot_secret,
            notification_chat_id: form.notification_chat_id
          }
        : {
            webhook_url: form.webhook_url,
            events: form.events,
            message_format: form.message_format,
            mention_all: form.mention_all
          })
    };
    await request(
      `/platforms/${id}/bindings${existing.value ? "/" + existing.value.id : ""}`,
      existing.value ? "PATCH" : "POST",
      body
    );
    dialog.value = false;
    if (store.currentId === id) await store.refresh();
  }, "平台绑定已保存");
  busy.value = false;
}
async function remove(binding: Binding) {
  try {
    await ElMessageBox.confirm(
      `删除「${binding.name}」的绑定？不会删除企业微信中的机器人`,
      "删除绑定",
      { type: "warning" }
    );
  } catch {
    return;
  }
  await perform(async () => {
    await request(
      `/platforms/${store.currentId}/bindings/${binding.id}`,
      "DELETE"
    );
    await store.refresh();
  }, "绑定已删除");
}
async function toggle(binding: Binding) {
  await perform(async () => {
    await request(
      `/platforms/${store.currentId}/bindings/${binding.id}`,
      "PATCH",
      { enabled: !binding.enabled }
    );
    await store.refresh();
  }, "状态已更新");
}
</script>
<template>
  <ConsolePage
    title="机器人与通知"
    description="聊天机器人与告警群各司其职；所有绑定只服务当前平台"
    ><template #actions
      ><el-button type="primary" @click="edit()"
        ><Plus :size="16" />新增绑定</el-button
      ></template
    >
    <section v-for="group in grouped" :key="group.kind" class="surface">
      <div class="section-heading">
        <div>
          <h2>{{ group.kind === "bot" ? "聊天机器人" : "群 Webhook" }}</h2>
          <p>
            {{
              group.kind === "bot"
                ? "在群聊与私聊中查询本平台数据"
                : "主动发送所订阅的告警和报告"
            }}
          </p>
        </div>
        <span class="mono muted">{{ group.items.length }} 个</span>
      </div>
      <el-empty v-if="!group.items.length" description="此平台还没有此类绑定"
        ><el-button
          @click="
            edit();
            form.kind = group.kind as 'bot' | 'webhook';
          "
          >添加绑定</el-button
        ></el-empty
      >
      <div
        v-for="binding in group.items"
        :key="binding.id"
        class="binding-line"
      >
        <div class="binding-icon">
          <Bot v-if="binding.kind === 'bot'" :size="20" /><Bell
            v-else
            :size="20"
          />
        </div>
        <div class="binding-copy">
          <h3>{{ binding.name }}</h3>
          <p>
            {{
              binding.kind === "webhook"
                ? events
                    .filter(e => binding.events.includes(e[0]))
                    .map(e => e[1])
                    .join(" · ")
                : "本平台群聊 / 私聊查询"
            }}
            · 凭证{{ binding.has_secret ? "已加密保存" : "未配置" }}
          </p>
        </div>
        <StatusDot
          :text="binding.enabled ? '已启用' : '已停用'"
          :tone="binding.enabled ? 'ok' : 'muted'"
        /><el-button link type="primary" @click="edit(binding)">编辑</el-button
        ><el-button link @click="toggle(binding)">{{
          binding.enabled ? "停用" : "启用"
        }}</el-button
        ><el-button link type="danger" @click="remove(binding)">删除</el-button>
      </div>
    </section>
    <el-dialog
      v-model="dialog"
      :title="existing ? '编辑平台绑定' : '新增平台绑定'"
      width="620px"
      destroy-on-close
      :close-on-click-modal="false"
      ><el-form label-position="top" @submit.prevent="save"
        ><el-form-item label="绑定名称"
          ><el-input
            v-model="form.name"
            maxlength="80"
            required /></el-form-item
        ><el-form-item label="绑定类型"
          ><el-radio-group v-model="form.kind" :disabled="!!existing"
            ><el-radio-button value="bot">聊天机器人</el-radio-button
            ><el-radio-button value="webhook"
              >群 Webhook</el-radio-button
            ></el-radio-group
          ></el-form-item
        ><template v-if="form.kind === 'bot'"
          ><el-form-item label="机器人 ID"
            ><el-input
              v-model="form.bot_id"
              :placeholder="
                existing ? '已保存，留空保留原值' : '企业微信智能机器人 bot_id'
              " /></el-form-item
          ><el-form-item label="机器人 Secret"
            ><el-input
              v-model="form.bot_secret"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="
                existing ? '已保存，留空保留原值' : '填写机器人 Secret'
              " /></el-form-item
          ><el-form-item label="余额主动提醒接收会话 ID（可选）"
            ><el-input
              v-model="form.notification_chat_id"
              placeholder="在目标企微会话发送“会话ID”，复制返回值"
            />
            <p class="section-footnote">
              仅用于显式绑定的上游余额提醒；留空则只支持聊天查询，不向所有会话广播。
            </p></el-form-item
          ></template
        ><template v-else
          ><el-form-item label="群 Webhook 地址"
            ><el-input
              v-model="form.webhook_url"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="
                existing
                  ? '已保存，留空保留原值'
                  : '企微官方群机器人的完整 Webhook 地址'
              " /></el-form-item
          ><el-form-item label="订阅通知类别"
            ><el-checkbox-group v-model="form.events"
              ><el-checkbox
                v-for="event in events"
                :key="event[0]"
                :value="event[0]"
                >{{ event[1] }}</el-checkbox
              ></el-checkbox-group
            ></el-form-item
          ><el-form-item label="消息格式"
            ><el-radio-group v-model="form.message_format"
              ><el-radio value="markdown">Markdown</el-radio
              ><el-radio value="text">文本</el-radio></el-radio-group
            ></el-form-item
          ><el-checkbox v-model="form.mention_all"
            >此群通知 @ 所有人（使用文本消息）</el-checkbox
          ></template
        ><el-form-item label="启用此绑定"
          ><el-switch v-model="form.enabled" /></el-form-item
        ><el-alert
          :title="`仅绑定「${store.current?.name}」；空密钥表示保留原值`"
          type="info"
          :closable="false"
          description="保存不发送测试消息。启用后会按规则连接机器人或发送监控通知"
        />
        <div class="form-footer">
          <el-button @click="dialog = false">取消</el-button
          ><el-button type="primary" native-type="submit" :loading="busy"
            >保存绑定</el-button
          >
        </div></el-form
      ></el-dialog
    ></ConsolePage
  >
</template>
