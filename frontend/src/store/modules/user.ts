import { defineStore } from "pinia";
import { store } from "@/store";
import { useSessionStoreHook } from "./session";
// Compatibility adapter for the upstream pure-admin layout. No JWT/localStorage auth.
export const useUserStore = defineStore("pure-user", {
  state: () => ({
    avatar: "",
    nickname: "",
    roles: [] as string[],
    permissions: [] as string[]
  }),
  getters: { username: () => useSessionStoreHook().username },
  actions: {
    async logOut() {
      await useSessionStoreHook().logout();
      location.assign("/login");
    }
  }
});
export const useUserStoreHook = () => useUserStore(store);
