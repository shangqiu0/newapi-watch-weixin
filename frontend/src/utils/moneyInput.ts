import type { Currency } from "@/api/types";

// Keep money as decimal text so browser number coercion cannot change thresholds.
export function normalizeYuan(value: string): string | null {
  const text = value.normalize("NFKC").trim().replace(/^¥\s*/, "");
  const match = /^(\d{1,18}|\d{1,3}(?:,\d{3}){1,5})(?:\.(\d{1,2}))?$/.exec(
    text
  );
  if (!match) return null;
  return `${BigInt(match[1].replaceAll(",", ""))}.${(match[2] ?? "").padEnd(2, "0")}`;
}

export function yuanForEditing(value?: string | null): string {
  const match = /^(\d+)(?:\.(\d+))?$/.exec(value ?? "");
  if (!match) return "";
  const fraction = (match[2] ?? "").padEnd(3, "0");
  const cents =
    BigInt(match[1]) * 100n +
    BigInt(fraction.slice(0, 2)) +
    (fraction[2] >= "5" ? 1n : 0n);
  return `${cents / 100n}.${String(cents % 100n).padStart(2, "0")}`;
}

export function readYuanThresholds(
  low: string,
  recovery: string
): { low: string; recovery: string | null } {
  const amount = normalizeYuan(low);
  const restored = recovery.trim() ? normalizeYuan(recovery) : null;
  if (amount === null || (recovery.trim() && restored === null)) {
    throw new Error("请输入非负人民币金额，最多两位小数，如 10000.00");
  }
  return { low: amount, recovery: restored };
}

export function validateCurrencyDraft(value: Currency): string {
  for (const [text, maximum, label] of [
    [value.quota_per_unit, 10n ** 18n, "1 USD 对应的 quota"],
    [value.usd_exchange_rate, 10n ** 12n, "1 USD 折合人民币"]
  ] as const) {
    const match = /^(\d{1,19})(?:\.(\d{1,12}))?$/.exec(text.trim());
    const whole = match ? BigInt(match[1]) : 0n;
    const fraction = match?.[2] ?? "";
    if (
      !match ||
      (whole === 0n && !/[1-9]/.test(fraction)) ||
      whole > maximum ||
      (whole === maximum && /[1-9]/.test(fraction))
    ) {
      return `${label}须为正数，最多 12 位小数，不使用分隔符或科学计数法`;
    }
  }
  return "";
}
