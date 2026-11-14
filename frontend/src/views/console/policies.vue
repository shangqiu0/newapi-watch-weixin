<script setup lang="ts">
import { ref } from "vue";
import { useMonitoring } from "@/composables/useMonitoring";
import { request, perform } from "@/api/console";
import ConsolePage from "@/components/Console/ConsolePage.vue";
import PolicyTargets from "@/components/Console/PolicyTargets.vue";
const state = useMonitoring(),
  config = state.config;
const tab = ref("channels"),
  preview = ref(""),
  previewing = ref(false);
async function showPreview(): Promise<void> {
  if (!config.value) return;
  previewing.value = true;
  await perform(async () => {
    preview.value = (
      await request<{ text: string }>(
        `/platforms/${state.platform.currentId}/alert-previews`,
        "POST",
        { rule: config.value.errors }
      )
    ).text;
  });
  previewing.value = false;
}
</script>
<template>
  <ConsolePage
    title="监控策略"
    description="设置检查对象、频率、触发条件与通知目标，保存后热更新，无需重连机器人"
    @refresh="state.refresh"
  >
    <template #actions
      ><el-button
        type="primary"
        :loading="state.saving.value"
        :disabled="!config"
        @click="state.save"
        >保存策略</el-button
      ></template
    >
    <el-alert
      v-if="state.error.value"
      :title="state.error.value"
      type="error"
      :closable="false"
    /><el-skeleton v-if="!config" :rows="9" />
    <template v-else>
      <el-tabs v-model="tab" class="policy-tabs">
        <el-tab-pane label="请求节流" name="requests"
          ><div class="policy-layout">
            <section class="surface">
              <div class="section-heading">
                <div>
                  <h2>平台 API 请求节流</h2>
                  <p>监控、分页查询、报表和机器人共享同一请求频率控制</p>
                </div>
              </div>
              <el-form v-if="config.requests" label-position="top"
                ><div class="form-grid">
                  <el-form-item label="请求最小间隔（秒）"
                    ><el-input-number
                      v-model="config.requests.min_interval_seconds"
                      :min="0.1"
                      :max="60"
                      :step="0.1"
                      :precision="1" /></el-form-item
                  ><el-form-item label="429 基础冷却（秒）"
                    ><el-input-number
                      v-model="config.requests.rate_limit_cooldown_seconds"
                      :min="5"
                      :max="3600"
                  /></el-form-item>
                </div>
                <el-alert
                  v-if="
                    state.data.value?.execution?.request_control
                      ?.cooldown_remaining
                  "
                  type="warning"
                  :closable="false"
                  :title="`当前平台请求冷却中，约 ${state.data.value.execution.request_control.cooldown_remaining} 秒后可重试`"
                />
                <p class="section-footnote">
                  默认请求至少间隔 1.2 秒。遇到 429
                  后停止该平台后续请求，不连续重试；上游提供 Retry-After
                  时取更长等待时间。保存或手动复查不会取消正在生效的冷却
                </p></el-form
              >
            </section>
            <aside class="policy-notes">
              <h3>减少限流，而不是绕过限流</h3>
              <p>
                如果平台同时被其他服务调用，请适当增加请求间隔和检查周期。不同平台分别节流，互不阻塞
              </p>
              <p>
                渠道快照和短期查询结果在同一平台内复用，翻页不反复扫描相同的整段日志或用户列表
              </p>
              <p>
                上游限额较低或共享出口 IP 限流时，仍可能收到
                429；系统会显示真实原因与冷却，而不是伪造空数据
              </p>
            </aside>
          </div></el-tab-pane
        >
        <el-tab-pane label="渠道状态" name="channels"
          ><div class="policy-layout">
            <section class="surface">
              <div class="section-heading">
                <div>
                  <h2>渠道状态检查</h2>
                  <p>完整读取渠道列表，确认自动禁用或渠道缺失</p>
                </div>
                <el-switch
                  v-model="config.channels.enabled"
                  aria-label="渠道状态策略启用"
                />
              </div>
              <el-form label-position="top"
                ><el-form-item label="监控渠道（留空检查全部）"
                  ><el-select
                    v-model="config.channels.channel_ids"
                    multiple
                    filterable
                    allow-create
                    style="width: 100%"
                    @change="
                      config.channels.channel_ids =
                        config.channels.channel_ids.map(Number)
                    "
                    ><el-option
                      v-for="channel in state.platform.overview?.channels ?? []"
                      :key="channel.id"
                      :value="channel.id"
                      :label="`${channel.name} #${channel.id}`" /></el-select
                ></el-form-item>
                <div class="form-grid">
                  <el-form-item label="检查间隔（秒）"
                    ><el-input-number
                      v-model="config.channels.poll_seconds"
                      :min="10"
                      :max="86400" /></el-form-item
                  ><el-form-item label="连续异常确认次数"
                    ><el-input-number
                      v-model="config.channels.confirmations"
                      :min="1"
                      :max="20" /></el-form-item
                  ><el-form-item label="重复提醒冷却（秒）"
                    ><el-input-number
                      v-model="config.channels.cooldown_seconds"
                      :min="30"
                      :max="86400" /></el-form-item
                  ><el-form-item label="完整列表缺失也告警"
                    ><el-switch v-model="config.channels.notify_missing"
                  /></el-form-item>
                </div>
                <div class="policy-switches">
                  <el-checkbox v-model="config.channels.notify_recovery"
                    >恢复时通知</el-checkbox
                  ><el-checkbox v-model="config.channels.mention_all"
                    >@ 所有人</el-checkbox
                  >
                </div>
                <el-form-item label="通知 Webhook"
                  ><PolicyTargets
                    v-model="config.channels.binding_ids"
                    kind="channel"
                /></el-form-item>
              </el-form>
            </section>
            <aside class="policy-notes">
              <h3>状态判断边界</h3>
              <p>
                自动禁用是异常；手动禁用不触发故障，也不作为故障已恢复的证据
              </p>
              <p>
                只有接口完整成功时才更新比较基线。令牌失效、超时或分页不完整，业务状态记为未知
              </p>
              <p>同一平台一轮只读取一次渠道列表，不按渠道重复发起请求</p>
            </aside>
          </div></el-tab-pane
        >
        <el-tab-pane label="请求错误" name="errors"
          ><div class="policy-layout">
            <section class="surface">
              <div class="section-heading">
                <div>
                  <h2>增量错误检查</h2>
                  <p>从检查游标读取新增错误，按渠道、模型、状态码聚合</p>
                </div>
                <el-switch
                  v-model="config.errors.enabled"
                  aria-label="错误策略启用"
                />
              </div>
              <el-form label-position="top"
                ><el-form-item label="触发状态码（可自定义 400–599）"
                  ><el-select
                    v-model="config.errors.status_codes"
                    multiple
                    filterable
                    allow-create
                    style="width: 100%"
                    @change="
                      config.errors.status_codes =
                        config.errors.status_codes.map(Number)
                    "
                    ><el-option
                      v-for="code in [
                        400, 401, 403, 404, 408, 429, 500, 502, 503, 504
                      ]"
                      :key="code"
                      :label="String(code)"
                      :value="code" /></el-select
                ></el-form-item>
                <div class="form-grid">
                  <el-form-item label="检查间隔（秒）"
                    ><el-input-number
                      v-model="config.errors.poll_seconds"
                      :min="30"
                      :max="86400" /></el-form-item
                  ><el-form-item label="冷却窗口（秒）"
                    ><el-input-number
                      v-model="config.errors.cooldown_seconds"
                      :min="30"
                      :max="86400" /></el-form-item
                  ><el-form-item label="同类错误最少次数"
                    ><el-input-number
                      v-model="config.errors.confirmations"
                      :min="1"
                      :max="20" /></el-form-item
                  ><el-form-item label="首次回看（秒，0 不追发历史）"
                    ><el-input-number
                      v-model="config.errors.initial_lookback_seconds"
                      :min="0"
                      :max="3600"
                  /></el-form-item>
                </div>
                <el-form-item label="限定渠道 ID"
                  ><el-select
                    v-model="config.errors.channel_ids"
                    multiple
                    allow-create
                    filterable
                    placeholder="留空匹配全部渠道"
                    style="width: 100%"
                    @change="
                      config.errors.channel_ids =
                        config.errors.channel_ids.map(Number)
                    "
                    ><el-option
                      v-for="channel in state.platform.overview?.channels ?? []"
                      :key="channel.id"
                      :value="channel.id"
                      :label="`${channel.name} #${channel.id}`" /></el-select
                ></el-form-item>
                <div class="form-grid">
                  <el-form-item label="限定模型"
                    ><el-select
                      v-model="config.errors.models"
                      multiple
                      allow-create
                      filterable
                      placeholder="留空匹配全部模型" /></el-form-item
                  ><el-form-item label="限定错误码"
                    ><el-select
                      v-model="config.errors.error_codes"
                      multiple
                      allow-create
                      filterable
                      placeholder="留空匹配全部错误码"
                  /></el-form-item>
                </div>
                <el-form-item label="通知 Webhook"
                  ><PolicyTargets
                    v-model="config.errors.binding_ids"
                    kind="request_error" /></el-form-item
                ><el-checkbox v-model="config.errors.mention_all"
                  >@ 所有人（使用企微 text 消息真正提及）</el-checkbox
                >
                <el-form-item label="通知模板"
                  ><el-input
                    v-model="config.errors.template"
                    type="textarea"
                    :rows="9"
                    maxlength="2500"
                    show-word-limit
                /></el-form-item>
                <p class="section-footnote">
                  占位符：{channel_name}、{channel_id}、{status_code}、{model}、{error_message}、{count}、{log_time}、{log_id}、{upstream_request_id}、{request_id}。日志时间取自原日志
                  created_at，与当前平台日志页使用相同的显示时区，不额外偏移时间；日志
                  ID 用于核对原记录。{request_id} 为平台请求 ID，不是上游请求
                  ID。旧模板中的错误码和告警时间行会自动移除，缺少的追踪字段自动补入；缺失字段显示“未提供”。
                </p>
                <el-collapse
                  ><el-collapse-item title="增量窗口设置" name="window"
                    ><div class="form-grid">
                      <el-form-item label="单次最多追赶（秒）"
                        ><el-input-number
                          v-model="config.errors.window_seconds"
                          :min="30"
                          :max="3600" /></el-form-item
                      ><el-form-item label="重叠窗口（秒）"
                        ><el-input-number
                          v-model="config.errors.overlap_seconds"
                          :min="0"
                          :max="300"
                      /></el-form-item></div></el-collapse-item
                ></el-collapse>
                <div class="form-footer">
                  <el-button :loading="previewing" @click="showPreview"
                    >预览消息（不发送）</el-button
                  >
                </div>
              </el-form>
            </section>
            <aside class="policy-notes">
              <h3>错误事件不是渠道故障状态</h3>
              <p>
                一个错误只计一次；冷却后汇总新增次数。停止出现错误不会自动产生“渠道恢复”
              </p>
              <p>
                默认首次从当前时刻建基线，不补发此前的错误。回看设置仅在新基线建立时生效
              </p>
              <p>
                只根据平台日志中实际记录的 HTTP
                状态码匹配，不凭空推断。上游未保留的错误无法补造
              </p>
              <pre v-if="preview" class="notification-preview">{{
                preview
              }}</pre>
            </aside>
          </div></el-tab-pane
        >
        <el-tab-pane label="检查失败" name="source"
          ><div class="policy-layout">
            <section class="surface">
              <div class="section-heading">
                <div>
                  <h2>监控来源不可用提醒</h2>
                  <p>接口无法检查时发服务告警，与渠道和余额告警分开</p>
                </div>
                <el-switch
                  v-model="config.source.enabled"
                  aria-label="来源失败策略启用"
                />
              </div>
              <el-form label-position="top"
                ><div class="form-grid">
                  <el-form-item label="连续失败确认次数"
                    ><el-input-number
                      v-model="config.source.confirmations"
                      :min="1"
                      :max="20" /></el-form-item
                  ><el-form-item label="重复提醒冷却（秒）"
                    ><el-input-number
                      v-model="config.source.cooldown_seconds"
                      :min="30"
                      :max="86400"
                  /></el-form-item>
                </div>
                <div class="policy-switches">
                  <el-checkbox v-model="config.source.notify_recovery"
                    >接口恢复时通知</el-checkbox
                  ><el-checkbox v-model="config.source.mention_all"
                    >@ 所有人</el-checkbox
                  >
                </div>
                <el-checkbox v-model="config.source.repeat_notifications"
                  >持续失败按冷却周期重复提醒（默认关闭）</el-checkbox
                >
                <el-form-item label="服务告警 Webhook"
                  ><PolicyTargets
                    v-model="config.source.binding_ids"
                    kind="service" /></el-form-item
              ></el-form>
            </section>
            <aside class="policy-notes">
              <h3>失败不等于业务归零</h3>
              <p>
                HTTP
                401/403、429、超时与不完整分页，都显示检查失败及最近成功时间
              </p>
              <p>
                重试按检查间隔指数退避，最长 15
                分钟。不覆盖渠道或用户的上次已确认状态
              </p>
            </aside>
          </div></el-tab-pane
        >
      </el-tabs>
      <div class="section-footnote">
        用户余额阈值在「余额监控」中逐人配置；日报周报在「报告调度」中独立启停
      </div>
    </template>
  </ConsolePage>
</template>
<style scoped>
.policy-layout {
  display: grid;
  grid-template-columns: minmax(0, 760px) minmax(240px, 1fr);
  gap: 32px;
  align-items: start;
}

.policy-notes {
  padding: 24px 0;
  font-size: 13px;
  line-height: 1.8;
  color: var(--text-2);
}

.policy-notes h3 {
  margin-bottom: 16px;
  font-size: 14px;
  font-weight: 500;
  color: var(--text);
}

.policy-notes p {
  margin-bottom: 16px;
}

.policy-switches {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  margin-bottom: 24px;
}

.policy-tabs {
  margin-top: 8px;
}

@media (width <= 1100px) {
  .policy-layout {
    grid-template-columns: 1fr;
    gap: 0;
  }

  .policy-notes {
    padding-top: 0;
  }
}
</style>
