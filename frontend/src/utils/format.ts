export function number(value: number | string | undefined | null): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(
    Number(value ?? 0)
  );
}
export function timeCN(
  value: string | number | null | undefined,
  timezone?: string
): string {
  if (value === null || value === undefined || value === "") return "尚无记录";
  const date = new Date(
    typeof value === "number" || /^\d+(\.\d+)?$/.test(value)
      ? Number(value) * 1000
      : value
  );
  if (Number.isNaN(date.getTime())) return "时间未知";
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: timezone,
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23"
  }).formatToParts(date);
  const get = (type: string) => parts.find(p => p.type === type)?.value ?? "";
  return `${get("year")}年${get("month")}月${get("day")}日 ${get("hour")}:${get("minute")}:${get("second")}`;
}
export function shortDate(value: string): string {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${Number(match[2])}月${Number(match[3])}日` : value;
}
export const statusNames: Record<number, string> = {
  0: "未知",
  1: "启用",
  2: "手动禁用",
  3: "自动禁用"
};
export function safeRedirect(value: unknown): string {
  return typeof value === "string" &&
    value.startsWith("/console/") &&
    !value.includes("//")
    ? value
    : "/console/overview";
}

export function alertState(record: { state: string; key?: string }): {
  text: string;
  tone: "ok" | "warn" | "muted";
} {
  if (record.state === "event" || record.key?.startsWith("request_error:"))
    return { text: "错误事件", tone: "warn" };
  if (record.key && !record.key.startsWith("policy:"))
    return { text: "旧策略记录", tone: "muted" };
  return record.state === "resolved"
    ? { text: "已恢复", tone: "ok" }
    : { text: "未恢复", tone: "warn" };
}
