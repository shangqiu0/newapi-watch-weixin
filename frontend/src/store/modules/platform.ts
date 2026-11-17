import { useStorage } from "@vueuse/core";
import { defineStore } from "pinia";
import { request } from "@/api/console";
import type { Overview, Platform } from "@/api/types";
import { store } from "@/store";
import { canChangePlatform } from "@/utils/platformGuard";
export const usePlatformStore = defineStore("console-platform", {
  state: () => ({
    list: [] as Platform[],
    currentId: useStorage("newapi-watch:selected-platform", "", undefined, {
      listenToStorageChanges: false,
      writeDefaults: false,
      flush: "sync"
    }),
    overview: null as Overview | null,
    loading: false,
    error: "",
    generation: 0,
    selectionGeneration: 0,
    listGeneration: 0,
    controller: null as AbortController | null,
    updated: 0
  }),
  getters: { current: state => state.list.find(p => p.id === state.currentId) },
  actions: {
    async load(preferred?: string) {
      const sequence = ++this.listGeneration;
      const platforms = await request<Platform[]>("/platforms");
      if (sequence !== this.listGeneration) return;
      this.list = platforms;
      const id = this.list.some(p => p.id === preferred)
        ? preferred
        : this.list.some(p => p.id === this.currentId)
          ? this.currentId
          : (this.list[0]?.id ?? "");
      await this.select(id);
    },
    async remove(id: string, revision?: number): Promise<void> {
      const version = revision === undefined ? "" : `?revision=${revision}`;
      await request(`/platforms/${encodeURIComponent(id)}${version}`, "DELETE");
      this.listGeneration++;
      this.list = this.list.filter(platform => platform.id !== id);
      if (this.currentId === id) await this.select(this.list[0]?.id ?? "");
    },
    async select(id: string) {
      const selection = ++this.selectionGeneration;
      if (id !== this.currentId && !(await canChangePlatform())) return;
      if (selection !== this.selectionGeneration) return;
      this.currentId = id;
      this.overview = null;
      await this.refresh();
    },
    async refresh() {
      const seq = ++this.generation;
      this.controller?.abort();
      this.controller = new AbortController();
      if (!this.currentId) {
        this.loading = false;
        return;
      }
      this.loading = true;
      this.error = "";
      const id = this.currentId;
      try {
        const data = await request<Overview>(
          `/platforms/${id}/overview`,
          "GET",
          undefined,
          this.controller.signal
        );
        if (seq === this.generation && id === this.currentId) {
          this.overview = data;
          this.updated = Date.now() / 1000;
        }
      } catch (error) {
        if (seq === this.generation && !this.controller.signal.aborted)
          this.error = error instanceof Error ? error.message : "读取失败";
      } finally {
        if (seq === this.generation) this.loading = false;
      }
    },
    clear() {
      this.selectionGeneration++;
      this.generation++;
      this.listGeneration++;
      this.controller?.abort();
      this.list = [];
      this.currentId = "";
      this.overview = null;
      this.error = "";
    }
  }
});
export const usePlatformStoreHook = () => usePlatformStore(store);
