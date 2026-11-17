/** One active draft guard; platform changes and route/tab changes use the same decision. */
let guard: (() => Promise<boolean>) | null = null;
export function registerPlatformGuard(
  callback: () => Promise<boolean>
): () => void {
  guard = callback;
  return () => {
    if (guard === callback) guard = null;
  };
}
export async function canChangePlatform(): Promise<boolean> {
  return guard ? guard() : true;
}
