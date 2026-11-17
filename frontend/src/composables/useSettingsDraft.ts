import { inject, ref, type Ref } from "vue";
export function useSettingsDraft(): Ref<boolean> {
  return inject<Ref<boolean>>("settingsDirty", ref(false));
}
