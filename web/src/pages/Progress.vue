<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import AppHeader from "../components/AppHeader.vue";
import Listen from "../components/Listen.vue";
import {
  CHARGE,
  get,
  type DeviceStatus,
  type RecorderStatus,
  type Status,
  type Upgrade,
} from "../lib/api";
import { upgradeText } from "../lib/recorderLog";
import { useSocket } from "../lib/socket";

// What the server is doing per device: how far its audio has arrived, how far it is finalized, what is left and
// when it should be done; refreshed by the status the socket sends every 2 s.
const status = ref<Status | null>(null);
onMounted(async () => (status.value = await get<Status>("/api/status")));
useSocket(
  (m) => {
    if (m.type === "status") status.value = m;
  },
  async () => (status.value = await get<Status>("/api/status")),
);

const devices = computed(() =>
  [...(status.value?.devices ?? [])].sort((a, b) =>
    (a.received_to ?? "") < (b.received_to ?? "") ? 1 : -1,
  ),
);
// each device's last firmware update in 30 days, looked up when the devices change
const ups = ref<Record<string, Upgrade | undefined>>({});
watch(
  () => devices.value.map((d) => d.device).join("\n"),
  async (names) => {
    const since = new Date(Date.now() - 30 * 86400_000).toISOString().slice(0, 10);
    for (const device of names ? names.split("\n") : [])
      ups.value[device] = (await get<Upgrade[]>("/api/upgrades", { device, since })).at(-1);
  },
);
const at = (iso: string | null) => (iso ? `${iso.slice(5, 10)} ${iso.slice(11, 16)}` : "—");
function span(s: number) {
  const m = Math.round(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)} 小时 ${m % 60} 分` : `${m} 分钟`;
}
const card = (h: RecorderStatus) =>
  h.store === -19
    ? "没有卡"
    : h.store === -28
      ? "已满，停止离线录音"
      : h.total_kb
        ? `剩 ${((h.free_kb ?? 0) / 1048576).toFixed(1)} / ${(h.total_kb / 1048576).toFixed(1)} GB`
        : "—";
const caughtUp = (d: DeviceStatus) => d.left_s < 30;
// Held (#130): the phone reads older audio off the recorder's card in the background while it sends what is said now;
// finals wait at the hold for it, so the server's own estimate does not apply
const held = (d: DeviceStatus) => {
  const kb = d.hw?.backlog_kb,
    eta = d.hw?.backlog_eta_s;
  return `定稿停在 ${at(d.hold!)}，等手机在后台补传卡里更早的录音${kb ? `（还有 ${(kb / 1024).toFixed(1)} MB${eta ? `，约 ${span(eta)}` : ""}）` : ""}，补完再接着定稿；初稿照常`;
};
const eta = (d: DeviceStatus) => {
  if (!d.eta_s) return "—";
  const t = new Date(Date.now() + d.eta_s * 1000);
  return `约 ${span(d.eta_s)}（${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}）`;
};
</script>

<template>
  <AppHeader />
  <main>
    <section v-for="d in devices" :key="d.device" class="card">
      <div class="top">
        <span
          ><b>{{ d.device }}</b>
          <RouterLink :to="`/device/${encodeURIComponent(d.device)}`">日志</RouterLink></span
        >
        <span :class="caughtUp(d) ? 'ok' : 'busy'">{{
          caughtUp(d) ? "已追上" : d.hold ? "等补传" : "处理中"
        }}</span>
      </div>
      <div class="grid">
        <span>已收到</span><span>{{ at(d.received_to) }}</span> <span>已定稿</span
        ><span>{{ at(d.done_to) }}</span> <span>待处理</span
        ><span>{{ caughtUp(d) ? "—" : span(d.left_s) + " 录音" }} · 排队 {{ d.queued }} 块</span>
        <template v-if="d.hold"
          ><span>补传</span><span>{{ held(d) }}</span></template
        >
        <span>速度</span><span>{{ d.rate ? `${d.rate} 倍实时` : "—" }}</span> <span>预计完成</span
        ><span>{{ caughtUp(d) ? "—" : d.hold ? "补传完之后" : eta(d) }}</span> <span>最近一轮</span
        ><span :class="{ bad: d.refine?.error }">{{
          d.refine?.error
            ? "失败：" + d.refine.error
            : d.refine
              ? `${d.refine.at.slice(11)} · ${(d.refine.ms / 1000).toFixed(0)} s`
              : "—"
        }}</span>
        <span>对话</span
        ><span>{{
          d.conversation
            ? `进行中，${d.conversation.start.slice(11, 16)} 起 · ${d.conversation.sentences} 句`
            : "无"
        }}</span>
        <template v-if="d.hw">
          <span>录音设备</span
          ><span>{{ [d.hw.model, d.hw.fw && "固件 " + d.hw.fw].filter(Boolean).join(" · ") }}</span>
          <span>电量</span
          ><span>{{
            [d.hw.battery != null && d.hw.battery + "%", d.hw.charge && CHARGE[d.hw.charge]]
              .filter(Boolean)
              .join(" · ") || "—"
          }}</span>
          <span>存储卡</span><span :class="{ bad: d.hw.store }">{{ card(d.hw) }}</span>
          <span>上报于</span><span>{{ at(d.hw.at) }}</span>
        </template>
        <template v-if="ups[d.device]">
          <span>最近升级</span
          ><span :class="{ bad: ups[d.device]!.ok === false }"
            >{{ at(ups[d.device]!.at) }} {{ upgradeText(ups[d.device]!) }}</span
          >
        </template>
      </div>
      <Listen class="listen" :device="d.device" />
    </section>
    <section v-if="status" class="card">
      <div class="top"><b>摘要</b></div>
      <div class="grid">
        <span>等待生成</span><span>{{ status.summaries_pending ?? 0 }} 段对话</span>
      </div>
    </section>
    <div v-if="status && !devices.length" class="muted">还没有设备的处理任务</div>
    <div v-if="!status" class="muted">加载中…</div>
  </main>
</template>

<style scoped>
.card {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px 14px;
  margin: 12px 0;
}
.top {
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
}
.ok {
  color: var(--accent);
  font-size: 13px;
}
.busy {
  color: var(--warm);
  font-size: 13px;
}
.grid {
  display: grid;
  grid-template-columns: 5em 1fr;
  gap: 2px 12px;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.listen {
  margin-top: 8px;
}
.grid span:nth-child(odd) {
  color: var(--muted);
}
</style>
