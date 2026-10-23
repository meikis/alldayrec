import vue from "@vitejs/plugin-vue";
import { defineConfig, lazyPlugins } from "vite-plus";

// dev proxies /api and /ws to a running server: a local one by default, or yours with its token from the shell:
//   LIVE_SERVER=https://your.server LIVE_RECORD_TOKEN=... pnpm dev
const target = process.env.LIVE_SERVER ?? "http://127.0.0.1:8770";
const headers = { Authorization: `Bearer ${process.env.LIVE_RECORD_TOKEN ?? ""}` };

export default defineConfig({
  fmt: {},
  lint: {
    jsPlugins: [{ name: "vite-plus", specifier: "vite-plus/oxlint-plugin" }],
    rules: { "vite-plus/prefer-vite-plus-imports": "error" },
    options: { typeAware: true, typeCheck: true },
  },
  plugins: lazyPlugins(() => [vue()]),
  build: { outDir: "../server/static/dist", emptyOutDir: true },
  server: {
    proxy: {
      "/api": { target, changeOrigin: true, headers },
      "/ws": { target, changeOrigin: true, ws: true, headers },
    },
  },
});
