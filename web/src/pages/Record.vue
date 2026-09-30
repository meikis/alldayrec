<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";

// Every SLICE_MS the recorder is stopped and restarted, so each blob is a complete webm file that
// POSTs to /api/chunks with its own start time, the same way the app sends its Opus pieces.
const SLICE_MS = 20000,
  BPS = 32000;
const device = ref(localStorage.device || "phone"),
  log = ref(""),
  sent = ref(0),
  queued = ref(0),
  recording = ref(false);
let rec: MediaRecorder | null = null,
  stream: MediaStream | null = null,
  sending = false,
  fails = 0;
const queue: { blob: Blob; start: number }[] = [];
const stat = computed(() =>
  recording.value ? `录音中 · 已传 ${sent.value} 块 · 待传 ${queued.value}` : "已停止",
);
const note = (s: string) =>
  (log.value = (new Date().toLocaleTimeString() + " " + s + "\n" + log.value).slice(0, 4000));
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function send(url: string, body?: Blob, type?: string): Promise<unknown> {
  const r = await fetch(url, {
    method: "POST",
    body,
    headers: type ? { "Content-Type": type } : {},
  });
  if (r.status === 429) {
    await sleep(20000); // server catching up
    return send(url, body, type);
  }
  if (!r.ok)
    throw new Error("HTTP " + r.status + " " + ((await r.json().catch(() => ({}))).detail || ""));
  return r.json();
}
async function drain() {
  // in start order, one at a time, retried until sent
  if (sending) return;
  sending = true;
  while (queue.length) {
    const { blob, start } = queue[0];
    try {
      await send(
        `/api/chunks?device=${encodeURIComponent(device.value)}&start=${start}`,
        blob,
        "audio/webm",
      );
      queue.shift();
      sent.value++;
      fails = 0;
    } catch (e) {
      fails++;
      note(`上传失败 ${(e as Error).message}，${Math.min(fails, 6) * 5} 秒后重试`);
      await sleep(Math.min(fails, 6) * 5000);
    }
    queued.value = queue.length;
  }
  sending = false;
}
function piece(): MediaRecorder {
  const r = new MediaRecorder(stream!, {
      mimeType: "audio/webm;codecs=opus",
      audioBitsPerSecond: BPS,
    }),
    start = Date.now();
  r.ondataavailable = (e) => {
    if (e.data.size) {
      queue.push({ blob: e.data, start });
      queued.value = queue.length;
      void drain();
    }
  };
  r.onerror = (e) => note("recorder error " + String((e as ErrorEvent).error ?? e));
  r.start();
  setTimeout(() => {
    if (rec === r) {
      r.stop();
      rec = piece();
    }
  }, SLICE_MS);
  return r;
}
async function start() {
  localStorage.device = device.value;
  stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    },
  });
  const track = stream.getAudioTracks()[0];
  track.onended = () => note("麦克风被系统收回，录音停了");
  rec = piece();
  sent.value = 0;
  recording.value = true;
  note(`开始 · ${track.label} · ${JSON.stringify(track.getSettings())}`);
}
async function stop() {
  const r = rec!,
    at = Date.now();
  rec = null;
  recording.value = false;
  r.stop();
  stream!.getTracks().forEach((t) => t.stop());
  note("停止，等最后几块传完");
  await sleep(500);
  while (queue.length || sending) await sleep(500);
  await send(`/api/chunks/end?device=${encodeURIComponent(device.value)}&t=${at}`).catch((e) =>
    note("结束信号失败 " + e.message),
  );
  note("已停止");
}
const toggle = () => (rec ? stop() : start().catch((e) => note("启动失败 " + e)));
const onVisible = () =>
  rec && note(`页面${document.hidden ? "进后台" : "回前台"}，已传 ${sent.value} 块`);
onMounted(() => document.addEventListener("visibilitychange", onVisible));
onUnmounted(() => document.removeEventListener("visibilitychange", onVisible));
</script>

<template>
  <div class="page">
    <div class="top">
      <div class="ctl">
        <input v-model="device" placeholder="设备名" />
        <button :class="{ on: recording }" @click="toggle">
          {{ recording ? "停止" : "开始录音" }}
        </button>
      </div>
      <p class="stat">{{ stat }}</p>
      <details>
        <summary>日志</summary>
        <div class="log">{{ log }}</div>
      </details>
    </div>
    <iframe src="/" title="字幕"></iframe>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
}
.top {
  padding: 10px 16px 6px;
  border-bottom: 1px solid var(--line);
}
.ctl {
  display: flex;
  gap: 10px;
  align-items: center;
}
.ctl button {
  font-size: 16px;
  padding: 10px 22px;
  border: 0;
  border-radius: 10px;
  background: #3b4cca;
  color: #fff;
  white-space: nowrap;
}
.ctl button.on {
  background: #c0392b;
}
.ctl input {
  font-size: 15px;
  padding: 8px 10px;
  flex: 1;
  min-width: 0;
  border: 1px solid #ccc;
  border-radius: 8px;
}
.stat {
  color: #555;
  font-size: 13px;
  margin: 6px 0 0;
}
details {
  font-size: 12px;
  color: #888;
  margin-top: 4px;
}
.log {
  font:
    12px ui-monospace,
    monospace;
  white-space: pre-wrap;
  color: #666;
  max-height: 30vh;
  overflow: auto;
}
iframe {
  flex: 1;
  border: 0;
  width: 100%;
}
</style>
