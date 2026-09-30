import vue from "@vitejs/plugin-vue";
import { defineConfig, lazyPlugins } from "vite-plus";

// dev reads and writes the live server (decided 09-30: still in development); the token comes from the shell:
//   LIVE_RECORD_TOKEN=... pnpm dev
const target = process.env.LIVE_SERVER ?? "https://lr.example.com";
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
