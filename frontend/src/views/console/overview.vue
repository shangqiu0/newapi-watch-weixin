<script setup lang="ts">
import { computed, onScopeDispose } from "vue";
import { usePlatformStore } from "@/store/modules/platform";
import { useMonitorRun } from "@/composables/useMonitorRun";
import { number, timeCN, alertState } from "@/utils/format";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import MonitorChecks from "@/components/Console/MonitorChecks.vue";
import StatusDot from "@/components/Console/StatusDot.vue";
const store = usePlatformStore();
const recheck = useMonitorRun();
const execution = computed(() => store.overview?.monitoring);
const summary = computed(() => execution.value?.summary);
const timer = window.setInterval(() => {
  if (!store.loading && !document.hidden) void store.refresh();
}, 15000);
onScopeDispose(() => window.clearInterval(timer));
</script>
<template>
  <ConsolePage
    title="监控工作台"
    description="按策略检查绑定平台，异常经确认后发送通知；监控不依赖大模型"
  >
    <template #actions
      ><el-button @click="$router.push('/console/policies')">配置策略</el-button
      ><el-popconfirm
        title="立即复查已启用策略？命中条件会发送正式告警"
        @confirm="recheck.run"
        ><template #reference
          ><el-button
            type="primary"
            :disabled="!store.current?.enabled"
            :loading="recheck.submitting.value"
            >立即复查</el-button
          ></template
        ></el-popconfirm
      ></template
    >
    <el-alert
      v-if="!store.current?.enabled"
      type="info"
      title="平台已停用，所有定时检查与机器人连接均已暂停"
      :closable="false"
      show-icon
    />
    <el-alert
      v-else-if="summary?.unknown"
      type="warning"
      title="部分检查失败或已逾期，相关业务状态未知"
      description="保留上次成功结果，不将接口失败当作零余额或渠道故障；失败检查会退避重试"
      :closable="false"
      show-icon
    />
    <el-alert
      v-if="summary?.historical_notifications"
      type="info"
      :title="`${number(summary.historical_notifications)} 条旧采集通知已暂停自动投递，可在「通知投递」查看并确认重发`"
      :closable="false"
      show-icon
    />
    <el-alert
      v-if="execution?.request_control?.cooldown_remaining"
      type="warning"
      :closable="false"
      :title="`上游限流冷却中，约 ${number(execution.request_control.cooldown_remaining)} 秒后恢复请求`"
      description="该平台的监控与人工查询共享冷却；其他平台不受影响。可在「监控策略 → 请求节流」调整频率"
      show-icon
    />
    <div class="kpi-strip monitor-summary">
      <div>
        <label>已启用检查</label
        ><strong
          >{{ summary ? number(summary.enabled) : "—"
          }}<span class="kpi-unit">
            / {{ summary ? number(summary.total) : "—" }}</span
          ></strong
        ><small>渠道、请求错误、余额与报告</small>
      </div>
      <div>
        <label>异常对象</label
        ><strong :class="{ 'error-text': summary?.active_incidents }">{{
          summary ? number(summary.active_incidents) : "—"
        }}</strong
        ><small>已确认且尚未恢复的状态</small>
      </div>
      <div>
        <label>检查失败 / 逾期</label
        ><strong>{{ summary ? number(summary.unknown) : "—" }}</strong
        ><small>与业务异常分开判断</small>
      </div>
      <div>
        <label>待发 / 投递失败</label
        ><strong
          >{{ summary ? number(summary.pending_notifications) : "—"
          }}<span class="kpi-unit">
            / {{ summary ? number(summary.failed_notifications) : "—" }}</span
          ></strong
        ><small>持久化队列，可追踪投递结果</small>
      </div>
    </div>
    <section class="surface monitor-workspace">
      <div class="section-heading">
        <div>
          <h2>检查计划</h2>
          <p>上次执行、下一次检查及来源失败 · 每 15 秒刷新状态</p>
        </div>
        <StatusDot
          :text="store.current?.enabled ? '自动检查已启用' : '平台已暂停'"
          :tone="store.current?.enabled ? 'ok' : 'muted'"
        />
      </div>
      <MonitorChecks
        :checks="execution?.checks ?? []"
        :timezone="store.current?.timezone"
      />
      <div class="data-provenance">
        <span>数据来源：本平台 NewAPI 管理接口</span
        ><span>仅保留监控状态，不持续同步业务日志</span>
      </div>
    </section>
    <div class="monitor-bottom-grid">
      <section class="surface">
        <div class="section-heading">
          <div>
            <h2>最近告警</h2>
            <p>请求错误是事件，不自动标记为恢复</p>
          </div>
          <el-button
            link
            type="primary"
            @click="$router.push('/console/alerts')"
            >全部记录</el-button
          >
        </div>
        <el-empty
          v-if="!store.overview?.alerts.items.length"
          description="暂无告警记录；请先确认监控策略已启用"
        />
        <div
          v-for="alert in store.overview?.alerts.items.slice(0, 5)"
          :key="alert.id"
          class="incident-line"
        >
          <StatusDot
            :text="alertState(alert).text"
            :tone="alertState(alert).tone"
          />
          <p>{{ alert.title.split("\n").slice(0, 2).join(" · ") }}</p>
          <small>{{ timeCN(alert.updated_at, store.current?.timezone) }}</small>
        </div>
      </section>
      <section class="surface">
        <div class="section-heading">
          <div>
            <h2>通知与连接</h2>
            <p>每个机器人与 Webhook 固定绑定当前平台</p>
          </div>
        </div>
        <dl class="monitor-connections">
          <div>
            <dt>企业微信机器人</dt>
            <dd class="mono">
              {{ store.overview?.health.bot.authenticated ?? 0 }} /
              {{ store.overview?.health.bot.configured ?? 0 }} 已认证
            </dd>
          </div>
          <div>
            <dt>已投递通知</dt>
            <dd class="mono">{{ number(execution?.queue.sent) }}</dd>
          </div>
          <div>
            <dt>死信通知</dt>
            <dd class="mono">{{ number(execution?.queue.dead) }}</dd>
          </div>
        </dl>
        <el-button @click="$router.push('/console/bindings')"
          >管理通知绑定</el-button
        ><el-button
          link
          type="primary"
          @click="$router.push('/console/notifications')"
          >投递记录</el-button
        >
        <p class="section-footnote">
          群聊与私聊用于人工查询；阈值与故障判断由规则执行，不交给大模型决定
        </p>
      </section>
    </div>
  </ConsolePage>
</template>
<style scoped>
.monitor-summary {
  margin-top: 24px;
}

.monitor-bottom-grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
  gap: 24px;
}

.monitor-workspace .data-provenance {
  flex-wrap: wrap;
  gap: 16px;
  margin-top: 20px;
}

.incident-line {
  padding: 16px 0;
  border-bottom: 1px solid var(--line);
}

.incident-line p {
  margin: 8px 0;
  font-size: 13px;
  overflow-wrap: anywhere;
}

.incident-line small {
  font-size: 12px;
  color: var(--muted);
}

.monitor-connections {
  margin-bottom: 24px;
}

.monitor-connections > div {
  display: flex;
  gap: 16px;
  justify-content: space-between;
  padding: 12px 0;
  font-size: 13px;
  border-bottom: 1px solid var(--line);
}

.monitor-connections dt {
  color: var(--text-2);
}

@media (width <= 1024px) {
  .monitor-bottom-grid {
    grid-template-columns: 1fr;
    gap: 0;
  }
}
</style>
