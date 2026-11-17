import {
  createRouter,
  createWebHistory,
  type RouteRecordRaw
} from "vue-router";
import routes from "./modules/console";
import remaining from "./modules/remaining";
import errorRoutes from "./modules/error";
import { useSessionStoreHook } from "@/store/modules/session";
import { usePermissionStoreHook } from "@/store/modules/permission";
import { useChatStore } from "@/store/modules/chat";
import { store } from "@/store";
import { usePlatformStoreHook } from "@/store/modules/platform";
import NProgress from "@/utils/progress";
export const constantRoutes: RouteRecordRaw[] = [routes as RouteRecordRaw];
export const constantMenus = [...routes.children, ...remaining];
export const remainingPaths = remaining.map(item => item.path);
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    ...constantRoutes,
    errorRoutes as RouteRecordRaw,
    ...(remaining as RouteRecordRaw[]),
    { path: "/:pathMatch(.*)*", redirect: "/error/404" }
  ],
  scrollBehavior: () => ({ top: 0 })
});
export function resetLoadedPaths() {}
export function resetRouter() {
  usePermissionStoreHook().clearAllCachePage();
}
router.beforeEach(async to => {
  NProgress.start();
  const session = useSessionStoreHook();
  if (to.path === "/login") {
    if (await session.ensure()) return "/console/overview";
    return true;
  }
  if (!(await session.ensure()))
    return { path: "/login", query: { redirect: to.fullPath } };
  usePermissionStoreHook().handleWholeMenus([]);
  if (!usePlatformStoreHook().list.length)
    try {
      await usePlatformStoreHook().load();
    } catch (error) {
      usePlatformStoreHook().error =
        error instanceof Error ? error.message : "无法加载平台";
    }
  document.title = `${String(to.meta.title ?? "管理台")} · NewAPI Watch`;
  return true;
});
router.afterEach(() => NProgress.done());
window.addEventListener("session-expired", () => {
  useSessionStoreHook().expire();
  usePlatformStoreHook().clear();
  if (router.currentRoute.value.path !== "/login")
    router.replace({
      path: "/login",
      query: { redirect: router.currentRoute.value.fullPath }
    });
});
window.addEventListener("console-session-cleared", () => {
  usePlatformStoreHook().clear();
  useChatStore(store).clear();
});
export default router;
