<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { oggOpus } from "../lib/ogg";

// Two sources, one queue. The phone's microphone: every SLICE_MS the recorder is stopped and restarted, so each
// blob is a complete webm file. The BLE recorder (Omi devkit firmware): its Opus frames go into Ogg pieces of
// SLICE_MS as they arrive, no re-encoding. Either way each piece POSTs to /api/chunks with its own start time,
// the same way the app sends its Opus pieces.
const SLICE_MS = 20000,
  BPS = 32000;
// Omi audio service: notify packets are [seq lo, seq hi, part] + Opus; a frame longer than the MTU comes in parts 1, 2…
const SVC = "19b10000-e8f2-537e-4f6c-d104768a1214",
  AUDIO = "19b10001-e8f2-537e-4f6c-d104768a1214",
  CODEC = "19b10002-e8f2-537e-4f6c-d104768a1214",
  OPUS_16K = 20,
  FRAME_MS = 10;
const device = ref(localStorage.device || "phone"),
  source = ref<"mic" | "ble">(localStorage.source === "ble" ? "ble" : "mic"),
  upload = ref(localStorage.upload !== "0"),
  log = ref(""),
  sent = ref(0),
  queued = ref(0),
  packets = ref(0),
  lastPiece = ref(""),
  recording = ref(false);
let rec: MediaRecorder | null = null,
  stream: MediaStream | null = null,
  sending = false,
  fails = 0;
const queue: { blob: Blob; start: number }[] = [];
const stat = computed(() =>
  recording.value
    ? `录音中 · ${source.value === "ble" ? `收到 ${packets.value} 包 · ` : ""}已传 ${sent.value} 块 · 待传 ${queued.value}`
    : "已停止",
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
        blob.type.split(";")[0],
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
function enqueue(blob: Blob, start: number) {
  if (lastPiece.value) URL.revokeObjectURL(lastPiece.value);
  lastPiece.value = URL.createObjectURL(blob); // the newest piece, to listen to on the page
  if (!upload.value) return;
  queue.push({ blob, start });
  queued.value = queue.length;
  void drain();
}
function piece(): MediaRecorder {
  const r = new MediaRecorder(stream!, {
      mimeType: "audio/webm;codecs=opus",
      audioBitsPerSecond: BPS,
    }),
    start = Date.now();
  r.ondataavailable = (e) => e.data.size && enqueue(e.data, start);
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
async function startMic() {
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
  note(`开始 · ${track.label} · ${JSON.stringify(track.getSettings())}`);
}

// --- BLE recorder. The device has no clock: a piece starts at the phone time its first frame arrived (never
// before the previous piece's end), and a lost packet or a dropped link starts a new piece, so later audio is
// never pulled earlier to close a gap.
let ble: BluetoothDevice | null = null,
  frames: Uint8Array[] = [],
  pieceStart = 0,
  lastEnd = 0,
  lastSeq = -1;
function cut() {
  if (!frames.length) return;
  enqueue(oggOpus(frames, FRAME_MS), pieceStart);
  lastEnd = pieceStart + frames.length * FRAME_MS;
  frames = [];
}
function onPacket(e: Event) {
  const v = (e.target as BluetoothRemoteGATTCharacteristic).value!;
  const b = new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
  const seq = b[0] | (b[1] << 8),
    part = b[2],
    data = b.slice(3);
  packets.value++;
  if (lastSeq >= 0 && ((seq - lastSeq) & 0xffff) !== 1) {
    note(`丢了 ${(seq - lastSeq - 1) & 0xffff} 个包，从这里起新的一块`);
    cut();
  }
  lastSeq = seq;
  if (part > 0) {
    // the rest of a frame; its start may have been lost, then the frame is too
    const head = frames.pop();
    if (head) frames.push(new Uint8Array([...head, ...data]));
    return;
  }
  if (frames.length >= SLICE_MS / FRAME_MS) cut();
  if (!frames.length) pieceStart = Math.max(Date.now(), lastEnd);
  frames.push(data);
}
async function connect() {
  const gatt = await ble!.gatt!.connect();
  const svc = await gatt.getPrimaryService(SVC);
  const codec = (await (await svc.getCharacteristic(CODEC)).readValue()).getUint8(0);
  if (codec !== OPUS_16K)
    throw new Error(`设备的编码是 ${codec}，这里只认 Opus 16 kHz（${OPUS_16K}）`);
  const ch = await svc.getCharacteristic(AUDIO);
  ch.oncharacteristicvaluechanged = onPacket; // assigned, not added: a reconnect hands back the same object
  await ch.startNotifications();
  lastSeq = -1;
  note(`已连上 ${ble!.name}，Opus 16 kHz`);
}
async function onDrop() {
  cut();
  if (!recording.value) return; // stopped on purpose
  note("蓝牙断了，重连中");
  for (let i = 1; recording.value && !ble?.gatt?.connected; i++) {
    try {
      await connect();
    } catch (e) {
      note(`重连失败 ${(e as Error).message}，${Math.min(i, 6) * 5} 秒后再试`);
      await sleep(Math.min(i, 6) * 5000);
    }
  }
}
async function startBle() {
  ble = await navigator.bluetooth.requestDevice({ filters: [{ services: [SVC] }] });
  ble.addEventListener("gattserverdisconnected", () => void onDrop());
  frames = [];
  lastEnd = 0;
  packets.value = 0;
  await connect();
}

async function start() {
  localStorage.device = device.value;
  localStorage.source = source.value;
  localStorage.upload = upload.value ? "1" : "0";
  sent.value = 0;
  recording.value = true;
  try {
    await (source.value === "ble" ? startBle() : startMic());
  } catch (e) {
    recording.value = false;
    throw e;
  }
}
async function stop() {
  const at = Date.now();
  recording.value = false;
  if (rec) {
    const r = rec;
    rec = null;
    r.stop();
    stream!.getTracks().forEach((t) => t.stop());
  }
  if (ble) {
    ble.gatt?.disconnect();
    ble = null;
    cut();
  }
  note("停止，等最后几块传完");
  await sleep(500);
  while (queue.length || sending) await sleep(500);
  if (upload.value)
    await send(`/api/chunks/end?device=${encodeURIComponent(device.value)}&t=${at}`).catch((e) =>
      note("结束信号失败 " + e.message),
    );
  note("已停止");
}
const toggle = () => (recording.value ? stop() : start().catch((e) => note("启动失败 " + e)));
const onVisible = () =>
  recording.value && note(`页面${document.hidden ? "进后台" : "回前台"}，已传 ${sent.value} 块`);
onMounted(() => document.addEventListener("visibilitychange", onVisible));
onUnmounted(() => document.removeEventListener("visibilitychange", onVisible));
</script>

<template>
  <div class="page">
    <div class="top">
      <div class="ctl">
        <input v-model="device" placeholder="设备名" />
        <select v-model="source" :disabled="recording">
          <option value="mic">本机麦克风</option>
          <option value="ble">蓝牙录音硬件</option>
        </select>
        <label><input v-model="upload" type="checkbox" :disabled="recording" />上传</label>
        <button :class="{ on: recording }" @click="toggle">
          {{ recording ? "停止" : "开始录音" }}
        </button>
      </div>
      <p class="stat">{{ stat }}</p>
      <audio v-if="lastPiece" :src="lastPiece" controls></audio>
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
.ctl label {
  font-size: 14px;
  white-space: nowrap;
}
.ctl select {
  font-size: 15px;
  padding: 8px 6px;
  border-radius: 8px;
}
audio {
  width: 100%;
  height: 32px;
  margin-top: 4px;
}
.ctl input:not([type="checkbox"]) {
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
