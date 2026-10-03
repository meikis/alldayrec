<script setup lang="ts">
import QRCode from "qrcode";
import { onMounted, ref } from "vue";
import AppHeader from "../components/AppHeader.vue";
import { get } from "../lib/api";

// The app's settings page scans this: server + token, so they don't need typing in by hand (manual entry stays).
// The token comes from the server, not from the cookie the browser holds (it's HttpOnly, page JS can't read it).
const qr = ref(""), error = ref("");
onMounted(async () => {
  try {
    const { token } = await get<{ token: string }>("/api/app-setup");
    const url = `liverecord://setup?server=${encodeURIComponent(location.origin)}&token=${encodeURIComponent(token)}`;
    qr.value = await QRCode.toDataURL(url, { width: 320, margin: 2 });
  } catch (e) {
    error.value = (e as Error).message;
  }
});
</script>

<template>
  <AppHeader />
  <main class="page">
    <p v-if="error" class="bad">二维码生成失败：{{ error }}</p>
    <template v-else>
      <div class="frame"><img v-if="qr" :src="qr" width="320" height="320" alt="登录二维码" /></div>
      <p class="muted">在 App 的「设置 › 扫码登录」里扫这个二维码，服务器地址和 Token 会自动填好</p>
    </template>
  </main>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding-top: 40px;
}
.frame {
  /* the QR is always black-on-white (scanners need that contrast); a fixed white box keeps it readable in dark mode too */
  background: #fff;
  padding: 16px;
  border-radius: 12px;
}
.frame img {
  display: block;
}
</style>
