<script setup lang="ts">
import { computed, ref, watch } from "vue";
import AppHeader from "../components/AppHeader.vue";
import LineChart from "../components/LineChart.vue";
import { CHARGE, get, type DeviceEvent, type DeviceLog, type Upgrade } from "../lib/api";
import {
  BOOT,
  DFU,
  DISCONNECT,
  FAULT,
  UPTIME,
  describe,
  millivolts,
  upgradeText,
  type Rec,
  type Said,
} from "../lib/recorderLog";

// One device's history: its battery (the mV its recorder logs every 10 min, the % its phone reads every minute) and
// what happened, the recorder's own event log and the app's, newest first, with each firmware update in a line.
const props = defineProps<{ name: string }>();
const RANGES: [string, number][] = [
  ["6 小时", 6],
  ["24 小时", 24],
  ["3 天", 72],
  ["7 天", 168],
];
const hours = ref(24);
const all = ref(false); // routine records too (heartbeats, connection parameters, ...)
const log = ref<DeviceLog | null>(null);
const ups = ref<Upgrade[]>([]);
const lo = ref(0),
  hi = ref(0);
const local = (t: number) =>
  new Date(t - new Date(t).getTimezoneOffset() * 60000).toISOString().slice(0, 23);
watch(
  [() => props.name, hours],
  async () => {
    document.title = `${props.name} · 设备日志`;
    hi.value = Date.now();
    lo.value = hi.value - hours.value * 3600_000;
    const q = { device: props.name, since: local(lo.value) };
    [log.value, ups.value] = await Promise.all([
      get<DeviceLog>("/api/device-log", q),
      get<Upgrade[]>("/api/upgrades", q),
    ]);
  },
  { immediate: true },
);

const rec = (e: DeviceEvent) => e.data as unknown as Rec;
function said(e: DeviceEvent): Said {
  if (e.src === "rec") return describe(rec(e));
  const { kind, text, level, ...rest } = e.data as { kind?: string; text?: string; level?: string };
  return {
    name: kind ?? "app",
    label: "",
    text:
      text ??
      Object.entries(rest)
        .map(([k, v]) => `${k} ${JSON.stringify(v)}`)
        .join("，"),
    level: level === "error" ? "bad" : level === "warn" ? "warn" : "",
    routine: false,
  };
}
const rows = computed(() => {
  const ev = log.value?.events ?? [];
  // a record its phone could not date sorts right after the one before it in the recorder's own order
  const key = new Map<DeviceEvent, string>();
  let last = "";
  for (const e of ev.filter((e) => e.src === "rec").sort((a, b) => a.seq - b.seq))
    key.set(e, (last = e.at ?? last));
  // an update's line just above its start
  const up = ups.value.map((u, i) => ({
    e: { src: "upgrade", seq: Number.MAX_SAFE_INTEGER - i, at: u.at, data: {} },
    key: u.at,
    name: "固件升级",
    label: "",
    text: upgradeText(u),
    level: (u.ok ? "" : u.ok == null ? "warn" : "bad") as Said["level"],
    routine: false,
  }));
  return ev
    .map((e) => ({ e, key: key.get(e) ?? e.at ?? "", ...said(e) }))
    .concat(up)
    .filter((r) => all.value || !r.routine)
    .sort((a, b) => (a.key !== b.key ? (a.key < b.key ? 1 : -1) : b.e.seq - a.e.seq));
});
const routine = computed(() => (log.value?.events ?? []).filter((e) => said(e).routine).length);

const dated = computed(() => (log.value?.events ?? []).filter((e) => e.src === "rec" && e.at));
const mv = computed(() =>
  dated.value
    .map((e) => ({ t: Date.parse(e.at!), v: millivolts(rec(e)) }))
    .filter((p) => p.v > 0)
    .sort((a, b) => a.t - b.t),
);
const pct = computed(() =>
  (log.value?.status ?? [])
    .filter((s) => s.battery != null)
    .map((s) => ({ t: Date.parse(s.at), v: s.battery! })),
);
// what it runs on, as its phone last read it
const charge = computed(() => (log.value?.status ?? []).filter((s) => s.charge).pop());
const NAMES: Record<number, string> = {
  [BOOT]: "重启",
  [FAULT]: "崩溃",
  [DFU]: "升级",
  [DISCONNECT]: "断开",
};
const marks = computed(() =>
  dated.value
    .map((e) => ({ t: Date.parse(e.at!), code: rec(e).code & ~UPTIME }))
    .filter((m) => NAMES[m.code])
    .map((m) => ({ t: m.t, label: NAMES[m.code], strong: m.code !== DISCONNECT })),
);
const when = (at: string) => (hours.value >= 24 ? `${at.slice(5, 10)} ` : "") + at.slice(11, 19);
</script>

<template>
  <AppHeader />
  <main>
    <div class="top">
      <h2>{{ name }}</h2>
      <span class="ranges">
        <button
          v-for="[l, h] in RANGES"
          :key="h"
          :class="['tag', { on: hours === h }]"
          @click="hours = h"
        >
          {{ l }}
        </button>
      </span>
    </div>
    <div v-if="!log" class="muted">加载中…</div>
    <template v-else>
      <section class="card">
        <div class="head">
          <b>电池电压</b><span class="muted">mV，录音设备每 10 分钟记一次</span>
        </div>
        <LineChart
          v-if="mv.length"
          :points="mv"
          :lo="lo"
          :hi="hi"
          unit="mV"
          :gap="30 * 60000"
          :marks="marks"
        />
        <div v-else class="muted">这段时间没有录音设备的日志（App 读到设备日志后才有）</div>
        <div v-if="marks.length" class="legend muted">
          <span><i class="solid" />重启 / 崩溃 / 升级</span><span><i class="dashed" />断开</span>
        </div>
      </section>
      <section class="card">
        <div class="head">
          <b>电量</b
          ><span class="muted"
            >%，手机连着时每分钟读一次{{
              charge ? `；${CHARGE[charge.charge!]}（${when(charge.at)}）` : ""
            }}</span
          >
        </div>
        <LineChart
          v-if="pct.length"
          :points="pct"
          :lo="lo"
          :hi="hi"
          unit="%"
          :gap="10 * 60000"
          :marks="marks"
        />
        <div v-else class="muted">这段时间手机没有上报电量</div>
      </section>
      <section class="card">
        <div class="head">
          <b>事件</b>
          <label v-if="routine" class="muted"
            ><input v-model="all" type="checkbox" /> 显示例行记录（{{ routine }} 条）</label
          >
        </div>
        <div v-for="r in rows" :key="r.e.src + r.e.seq" :class="['ev', r.level]">
          <span class="at">{{ r.e.at ? when(r.e.at) : "时间不明" }}</span>
          <span class="src">{{
            r.e.src === "rec" ? "设备" : r.e.src === "upgrade" ? "汇总" : "App"
          }}</span>
          <span
            ><b>{{ r.label || r.name }}</b> <span class="detail">{{ r.text }}</span></span
          >
        </div>
        <div v-if="!rows.length" class="muted">这段时间没有事件</div>
      </section>
    </template>
  </main>
</template>

<style scoped>
.top {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
}
h2 {
  margin: 0;
  font-size: 18px;
}
.ranges {
  display: flex;
  gap: 6px;
}
.card {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px 14px;
  margin: 12px 0;
  font-size: 13px;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
}
.legend {
  display: flex;
  gap: 16px;
  font-size: 12px;
}
.legend i {
  display: inline-block;
  width: 14px;
  vertical-align: middle;
  margin-right: 4px;
  border-top: 1px solid var(--warm);
}
.legend i.dashed {
  border-top: 1px dashed var(--muted);
}
.ev {
  display: grid;
  grid-template-columns: auto 2.5em 1fr;
  align-items: baseline;
  gap: 2px 10px;
  padding: 3px 0;
  border-top: 1px solid var(--soft);
  font-variant-numeric: tabular-nums;
}
.at,
.src,
.detail {
  color: var(--muted);
}
.ev.warn b {
  color: var(--warm);
}
.ev.bad b,
.ev.bad .detail {
  color: var(--err);
}
</style>
