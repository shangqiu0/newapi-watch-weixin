import { defineStore } from "pinia";
import { request, setCsrf } from "@/api/console";
import { store } from "@/store";
interface Session {
  username: string;
  csrf_token: string;
  demo: boolean;
}
export const useSessionStore = defineStore("console-session", {
  state: () => ({
    authenticated: false,
    username: "",
    csrf: "",
    demo: false,
    checkedAt: 0
  }),
  actions: {
    accept(value: Session) {
      this.authenticated = true;
      this.username = value.username;
      this.csrf = value.csrf_token;
      this.demo = value.demo;
      this.checkedAt = Date.now();
      setCsrf(value.csrf_token);
    },
    async ensure() {
      if (this.authenticated && Date.now() - this.checkedAt < 30000)
        return true;
      try {
        this.accept(await request<Session>("/session"));
        return true;
      } catch {
        this.expire();
        return false;
      }
    },
    async login(username: string, password: string) {
      this.accept(
        await request<Session>("/login", "POST", { username, password })
      );
    },
    expire() {
      this.$reset();
      setCsrf("");
      window.dispatchEvent(new Event("console-session-cleared"));
    },
    async logout() {
      await request("/logout", "POST");
      this.expire();
    }
  }
});
export const useSessionStoreHook = () => useSessionStore(store);
