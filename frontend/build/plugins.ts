import vue from "@vitejs/plugin-vue";
import vueJsx from "@vitejs/plugin-vue-jsx";
import tailwindcss from "@tailwindcss/vite";
import svgLoader from "vite-svg-loader";
import Icons from "unplugin-icons/vite";
import type { PluginOption } from "vite";
export function getPluginsList(_cdn?: boolean, _compression?: ViteCompression): PluginOption[] {
  return [vue(), vueJsx(), tailwindcss(), svgLoader(), Icons({compiler:"vue3", scale:1})];
}
