export interface LLM {
  enabled: boolean;
  compatibility: "auto" | "openai" | "deepseek";
  base_url: string;
  model: string;
  has_api_key: boolean;
}
export interface Platform {
  revision?: number;
  updated_at?: number;
  runtime_status?: "running" | "disabled" | "error";
  id: string;
  name: string;
  newapi_url: string;
  newapi_user_id: number;
  timezone: string;
  enabled: boolean;
  poll_seconds: number;
  log_seconds: number;
  llm: LLM;
  has_newapi_token: boolean;
}
export interface Channel {
  id: number;
  name: string;
  status: number;
  balance: number | null;
  response_time: number;
  models?: string;
  group?: string;
  priority?: number | null;
  weight?: number | null;
  used_quota?: number;
  test_time?: number;
  balance_updated_time?: number;
  type?: number;
  updated_at?: number;
}
export interface Metrics {
  success_records: number;
  error_records: number;
  quota: number;
  tokens: number;
  active_users: number;
  active_channels: number;
}
export interface Rank extends Metrics {
  key: string;
  label: string;
}
export interface Statistics {
  unavailable_metrics?: string[];
  summary: Metrics;
  ranking: Rank[];
  channel_leaders: Rank[];
  series: (Metrics & { label: string })[];
  coverage: {
    complete: boolean;
    from_timestamp: number | null;
    through_timestamp: number | null;
  };
  timezone: string;
  source?: string;
  queried_at?: number;
  limitation?: string;
}
export interface AlertRecord {
  key?: string;
  id: string;
  title: string;
  level: string;
  state: string;
  acknowledged: boolean;
  updated_at: number;
}
export interface AuditRecord {
  id: string;
  created_at: number;
  source: string;
  action: string;
  outcome: string;
  duration_ms: number;
}
export interface Binding {
  id: string;
  name: string;
  kind: "bot" | "webhook";
  enabled: boolean;
  has_secret: boolean;
  events: string[];
  message_format: "text" | "markdown";
  mention_all: boolean;
}
export interface Diagnostic {
  ok: boolean;
  code: string;
  message: string;
  http_status?: number;
  checked_at: number;
  compatibility?: string;
}
export interface Overview {
  platform: Platform;
  monitoring: MonitorExecution;
  channels: Channel[];
  statistics: Statistics;
  alerts: { items: AlertRecord[]; total: number };
  audits: { items: AuditRecord[]; total: number };
  bindings: Binding[];
  demo: boolean;
  model_status: Diagnostic | null;
  health: {
    status: string;
    channels_fresh: boolean;
    logs_fresh: boolean;
    channels_last_ok: string | null;
    logs_last_ok: string | null;
    reasons: string[];
    queue: Record<string, number>;
    bot: { configured: number; authenticated: number };
  };
}
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  source?: string;
  complete?: boolean;
  all_total?: number;
  summary?: Record<string, number>;
}
export interface PolicySettings {
  binding_ids: string[];
  confirmations: number;
  cooldown_seconds: number;
  notify_recovery: boolean;
  mention_all: boolean;
}
export interface ChannelRule extends PolicySettings {
  enabled: boolean;
  poll_seconds: number;
  channel_ids: number[];
  notify_missing: boolean;
}
export interface SourceRule extends PolicySettings {
  enabled: boolean;
  repeat_notifications: boolean;
}
export interface ErrorRule extends PolicySettings {
  poll_seconds: number;
  initial_lookback_seconds: number;
  window_seconds: number;
  overlap_seconds: number;
  enabled: boolean;
  status_codes: number[];
  channel_ids: number[];
  models: string[];
  error_codes: string[];
  cooldown_seconds: number;
  mention_all: boolean;
  template: string;
}
export interface BalanceRule extends PolicySettings {
  poll_seconds: number | null;
  recovery_threshold: number | null;
  user_id: number;
  name: string;
  threshold: number;
  enabled: boolean;
  notify_recovery: boolean;
  mention_all: boolean;
}
export interface Schedule {
  pages_per_run: number;
  max_failures: number;
  retry_seconds: number;
  binding_ids: string[];
  enabled: boolean;
  hour: number;
  minute: number;
  weekday: number;
  with_image: boolean;
}
export interface MonitoringConfig {
  requests: {
    min_interval_seconds: number;
    rate_limit_cooldown_seconds: number;
  };
  channels: ChannelRule;
  source: SourceRule;
  errors: ErrorRule;
  balances: BalanceRule[];
  balance_poll_seconds: number;
  daily: Schedule;
  weekly: Schedule;
}
export interface MonitoringData {
  execution: MonitorExecution;
  last_run: MonitorRun | null;
  config: MonitoringConfig;
  samples: {
    id: number;
    quota: number;
    threshold: number;
    is_low: boolean;
    updated_at: number;
  }[];
  errors: Record<string, string>;
}
export interface UserInfo {
  id: number;
  username: string;
  display_name: string;
  quota: number;
  used_quota: number;
  request_count: number;
  status: number;
  group: string;
}
export interface LogRecord {
  id: number;
  type: number;
  channel_id: number;
  channel_name: string;
  user_id: number;
  model_name: string;
  quota: number;
  prompt_tokens: number;
  completion_tokens: number;
  use_time: number;
  created_at: number;
  http_status: number;
  error_code: string;
  error_message: string;
}

export interface MonitorRun {
  id: string;
  status: "queued" | "completed" | "failed" | "interrupted";
  requested_at: number;
  finished_at?: number;
}
export interface MonitorCheck {
  id: string;
  name: string;
  kind: string;
  enabled: boolean;
  interval: number;
  status: "ok" | "unknown" | "pending" | "disabled";
  last_at?: number;
  last_ok_at?: number;
  next_at?: number;
  failures?: number;
  error?: string;
  stale: boolean;
  checking?: boolean;
  detail: Record<string, string | number | boolean>;
}
export interface MonitorExecution {
  request_control?: {
    min_interval_seconds: number;
    rate_limit_cooldown_seconds: number;
    cooldown_remaining: number;
    cooldown_until: number | null;
  };
  checks: MonitorCheck[];
  summary: {
    enabled: number;
    total: number;
    unknown: number;
    active_incidents: number;
    pending_notifications: number;
    failed_notifications: number;
    historical_notifications?: number;
  };
  incidents: {
    id: string;
    evidence: Record<string, string | number>;
    updated_at: number;
  }[];
  queue: Record<string, number>;
  mode: string;
  business_logs_persisted: boolean;
}
export interface QueryTask {
  id: string;
  status:
    | "queued"
    | "running"
    | "waiting"
    | "paused"
    | "failed"
    | "complete"
    | "cancelled";
  query: {
    start: string;
    end: string;
    dimension: string;
    metric: string;
    status_codes?: number[];
  };
  progress: {
    records_read: number;
    total_requests: number;
    restarts: number;
    from_timestamp?: number;
    through_timestamp?: number;
  };
  result: Statistics | null;
  error: string;
  next_at?: number;
  notified_at?: number;
}
export interface PendingStatistics {
  pending: true;
  task: QueryTask;
  limitation: string;
}
