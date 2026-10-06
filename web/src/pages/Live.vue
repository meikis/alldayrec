<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import AppHeader from "../components/AppHeader.vue";
import DayPicker from "../components/DayPicker.vue";
import MediaFold from "../components/MediaFold.vue";
import Timeline from "../components/Timeline.vue";
import TranscriptRow from "../components/TranscriptRow.vue";
import {
  fold,
  get,
  inSpans,
  hhmm,
  hhmmss,
  type Conversation,
  type LiveState,
  type LogEvent,
  type Row,
  type Status,
} from "../lib/api";
import { loadCalendar, today as todayOf } from "../lib/calendar";
import { useSocket } from "../lib/socket";

// One day at a time (?day=YYYY-MM-DD; none: today, which follows the live stream)
const GAP_MS = 5 * 60 * 1000; // a quiet stretch this long gets a mark, so conversations separate
const CATCHUP_MS = 2 * 60 * 1000; // final rows this far short of a device's drafts (or newest audio): a backlog being finalized
const route = useRoute(),
  router = useRouter();
const today = todayOf();
const day = computed(() => (route.query.day as string | undefined) || today),
  live = computed(() => day.value === today);
const rows = ref<Row[]>([]),
  convs = ref<Conversation[]>([]),
  events = ref<LogEvent[]>([]),
  status = ref<Status | null>(null);
const lives = ref<LiveState[]>([]),
  hit = ref("");
const ready = ref(false);
const follow = ref(true),
  logOpen = ref(false),
  logBox = ref<HTMLElement | null>(null);

type Item =
  | { k: "row"; key: string; r: Row; media?: boolean }
  | { k: "fold"; key: string; rows: Row[] }
  | { k: "conv"; key: string; c: Conversation }
  | { k: "gap"; key: string; at: string }
  | { k: "catchup"; key: string; device: string; from: string; to: string; eta: number | null };
// Per device, the time between its final rows and its drafts (or, with none, its newest audio): the drafts follow
// what is said now while a backlog (a phone back after hours offline) is finalized in order, so this time is not lost
const catchups = computed(() =>
  (live.value ? (status.value?.devices ?? []) : []).flatMap((d) => {
    const ft = finalTo[d.device] ?? ""; // a final message comes before the next status (every 2 s)
    const from = d.done_to && d.done_to > ft ? d.done_to : ft;
    if (!from) return [];
    const to =
      rows.value
        .filter((r) => r.state === "draft" && r.source === d.device && r.start >= from)
        .reduce<string | null>((a, r) => (a && a < r.start ? a : r.start), null) ?? d.received_to;
    if (!to || +new Date(to) - +new Date(from) <= CATCHUP_MS) return [];
    const it: Item = {
      k: "catchup",
      key: "u" + d.device,
      device: d.device,
      from,
      to,
      eta: d.eta_s,
    };
    return [{ s: from, it }];
  }),
);
const timeline = computed(() => {
  const ev = [
    ...rows.value.map((r) => ({ s: r.start, it: { k: "row", key: "s" + r.id, r } as Item })),
    ...convs.value.map((c) => ({ s: c.end, it: { k: "conv", key: "c" + c.id, c } as Item })),
    ...catchups.value,
  ].sort((a, b) => (a.s < b.s ? -1 : a.s > b.s ? 1 : 0)); // stable: a divider stays after rows starting at its end
  const out: Item[] = [];
  let lastEnd = 0;
  for (const { s, it } of ev) {
    if (it.k === "row") {
      if (lastEnd && +new Date(s) - lastEnd > GAP_MS) out.push({ k: "gap", key: "g" + s, at: s });
      lastEnd = +new Date(it.r.end);
    }
    out.push(it);
  }
  // media rows, known once their conversation is summarized, fold run by run between dividers
  const res: Item[] = [];
  let rs: Row[] = [];
  const flush = () => {
    for (const g of fold(rs, mediaOf))
      res.push(g.k === "row" ? { k: "row", key: "s" + g.r.id, r: g.r, media: g.media } : g);
    rs = [];
  };
  for (const it of out) {
    if (it.k === "row") rs.push(it.r);
    else {
      flush();
      res.push(it);
    }
  }
  flush();
  return res;
});
const mediaOf = (r: Row) =>
  convs.value.some(
    (c) =>
      (!c.device || c.device === r.source) &&
      c.start <= r.start &&
      r.start <= c.end &&
      inSpans(r, c.summary?.media_spans),
  );
const lastEvent = computed(() => {
  const e = events.value.at(-1);
  return e ? `· ${hhmmss(e.at)} ${e.text}`.slice(0, 90) : "";
});

// per device, up to where final rows cover the time: its drafts before that are no longer shown (they stay on the server)
const finalTo: Record<string, string> = {};
function finalize(m: { device?: string; from: string; to: string; rows: Row[] }) {
  if (m.device) finalTo[m.device] = m.to;
  rows.value = rows.value
    .filter((r) =>
      r.state === "draft"
        ? !(r.source === m.device && r.start < m.to)
        : !(m.from <= r.start && r.start < m.to && (!m.device || r.source === m.device)),
    )
    .concat(m.rows);
}
function addDrafts(m: { device: string; rows: Row[] }) {
  const to = finalTo[m.device] || "";
  rows.value = rows.value.concat(m.rows.filter((r) => r.start >= to));
}
function addConv(c: Conversation) {
  if (c.state === "open") return;
  const i = convs.value.findIndex((x) => x.id === c.id);
  if (i >= 0) convs.value[i] = c;
  else convs.value.push(c);
}
function addEvent(e: LogEvent) {
  events.value.push(e);
  if (events.value.length > 300) events.value.splice(0, events.value.length - 300);
}

async function loadDay() {
  const d = day.value;
  const [rs, cs, ls] = await Promise.all([
    get<Row[]>("/api/sentences", { date: d }),
    get<Conversation[]>("/api/conversations", { date: d }),
    get<LiveState[]>("/api/live"),
  ]);
  if (d !== day.value) return; // another day was picked meanwhile
  rows.value = rs;
  lives.value = ls;
  convs.value = [];
  cs.forEach(addConv);
  ready.value = true;
  await nextTick();
  if (live.value) toBottom();
  else window.scrollTo(0, 0);
}
const go = (d: string) => router.push({ query: d === today ? {} : { day: d } });
watch(day, () => {
  ready.value = false;
  follow.value = live.value;
  loadDay();
});

const atBottom = () => window.innerHeight + window.scrollY >= document.body.scrollHeight - 40;
const onScroll = () => (follow.value = atBottom());
const toBottom = () => window.scrollTo(0, document.body.scrollHeight);
watch(timeline, async () => {
  if (!follow.value || !live.value) return;
  await nextTick();
  toBottom();
});
watch(
  () => events.value.length,
  async () => {
    await nextTick();
    if (logOpen.value && logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight;
  },
);

onMounted(async () => {
  window.addEventListener("scroll", onScroll);
  loadCalendar();
  follow.value = live.value;
  await loadDay();
  (await get<LogEvent[]>("/api/events")).forEach(addEvent);
});
onUnmounted(() => window.removeEventListener("scroll", onScroll));

const { connected, send } = useSocket((m) => {
  // rows and conversations of another day than the one shown are not for this page
  if (m.type === "final") {
    if (live.value) finalize(m);
  } else if (m.type === "draft") {
    if (live.value) addDrafts(m);
  } else if (m.type === "conversation") {
    if (m.start.slice(0, 10) === day.value) addConv(m);
  } else if (m.type === "summary") {
    const c = convs.value.find((x) => x.id === m.conversation);
    if (c) c.summary = { ...m };
  } else if (m.type === "live") {
    lives.value = lives.value.filter((l) => l.device !== m.device).concat(m.live ? [m.live] : []);
  } else if (m.type === "event") addEvent(m);
  else status.value = m;
}, loadDay);

// The live analysis runs only while a page shows today in front: a heartbeat every 20 s, and a wake when
// the page comes back to the front so a stale analysis is redone at once.
let wake = true;
function beat() {
  if (live.value && !document.hidden && send({ type: "watch", wake })) wake = false;
}
function onVisible() {
  if (document.hidden) return;
  wake = true;
  beat();
}
let beatTimer = 0;
onMounted(() => {
  beatTimer = window.setInterval(beat, 20000);
  document.addEventListener("visibilitychange", onVisible);
});
onUnmounted(() => {
  clearInterval(beatTimer);
  document.removeEventListener("visibilitychange", onVisible);
});
watch([connected, live], ([c, l]) => c && l && ((wake = true), beat()));

// the most recently updated device's analysis, shown above the transcript in a pane whose height is dragged
const current = computed(() =>
  live.value
    ? lives.value.reduce<LiveState | null>((a, l) => (a && a.at > l.at ? a : l), null)
    : null,
);
const clampH = (h: number) => Math.round(Math.min(Math.max(h, 80), window.innerHeight * 0.7));
const paneH = ref(
  clampH(
    Number(
      (() => {
        try {
          return localStorage.getItem("livePaneH");
        } catch {
          return null;
        }
      })(),
    ) || 220,
  ),
);
const paneBox = ref<HTMLElement | null>(null);
let drag: { y: number; h: number } | null = null;
function dragStart(e: PointerEvent) {
  drag = { y: e.clientY, h: paneH.value };
  (e.target as HTMLElement).setPointerCapture(e.pointerId);
}
function dragMove(e: PointerEvent) {
  if (drag) paneH.value = clampH(drag.h + e.clientY - drag.y);
}
function dragEnd() {
  drag = null;
  try {
    localStorage.setItem("livePaneH", String(paneH.value));
  } catch {}
}
watch(
  () => current.value?.at,
  async () => {
    await nextTick();
    if (paneBox.value) paneBox.value.scrollTop = paneBox.value.scrollHeight; // the newest topic is at the bottom
  },
);
function jump(start: string) {
  hit.value = start;
  const el = document.querySelector(`[data-start="${CSS.escape(start)}"]`);
  if (!el) return;
  const head = document.querySelector("header")?.getBoundingClientRect().height ?? 0;
  window.scrollTo({
    top: el.getBoundingClientRect().top + window.scrollY - head - 8,
    behavior: "smooth",
  });
}

const convLink = (c: Conversation) =>
  c.summary?.status === "done"
    ? c.summary.title || "查看"
    : c.summary?.status === "failed"
      ? "摘要生成失败"
      : "摘要生成中…";
</script>

<template>
  <AppHeader in-app>
    <div class="head">
      <DayPicker :day="day" @pick="go">
        <button v-if="!live" class="back" @click="go(today)">回到今天</button>
      </DayPicker>
      <RouterLink class="status" to="/progress" title="处理进度">
        <template v-if="!connected">连接断开，重连中…</template>
        <template v-else-if="status">
          <span>{{ status.device || "等待录音" }}</span>
          <span :class="{ bad: (status.behind_s ?? 0) > 30 }"
            >落后 {{ status.behind_s == null ? "—" : status.behind_s + " 秒" }}</span
          >
          <span :class="{ bad: status.queued }">排队 {{ status.queued ?? 0 }} 块</span>
          <span :class="{ bad: (status.latency_s ?? 0) > 60 }"
            >上屏 {{ status.latency_s ?? "—" }} 秒</span
          >
          <span :class="{ bad: status.refine?.error }">
            分离
            {{
              status.refine
                ? status.refine.error
                  ? "失败"
                  : status.refine.at.slice(11) + " " + (status.refine.ms / 1000).toFixed(0) + "s"
                : "等待"
            }}
          </span>
          <span
            >状态机
            {{
              status.conversation
                ? "对话打开 " +
                  hhmmss(status.conversation.start) +
                  " · " +
                  status.conversation.sentences +
                  " 句"
                : "空闲，无打开的对话"
            }}</span
          >
          <span>{{ status.engine }} {{ status.commit }}</span>
        </template>
        <template v-else>连接中…</template>
      </RouterLink>
    </div>
    <div v-if="current" class="pane">
      <div ref="paneBox" class="pane-body" :style="{ height: paneH + 'px' }">
        <div class="pane-meta">
          实时分析 · 对话 {{ hhmmss(current.start) }} 起 · 更新于 {{ hhmmss(current.at) }} · 到
          {{ hhmmss(current.upto) }} 共 {{ current.sentences }} 句
        </div>
        <Timeline :topics="current.topics" open @jump="jump" />
      </div>
      <div
        class="grip"
        title="拖动调整高度"
        @pointerdown="dragStart"
        @pointermove="dragMove"
        @pointerup="dragEnd"
        @pointercancel="dragEnd"
      ></div>
    </div>
  </AppHeader>
  <main class="live">
    <template v-for="it in timeline" :key="it.key">
      <TranscriptRow
        v-if="it.k === 'row'"
        :row="it.r"
        :media="it.media"
        :hit="hit === it.r.start"
      />
      <MediaFold v-else-if="it.k === 'fold'" :rows="it.rows" :hit="hit" />
      <div v-else-if="it.k === 'conv'" class="conv">
        对话 {{ hhmmss(it.c.start) }}–{{ hhmmss(it.c.end) }} 结束 · 参与
        {{ it.c.participants.join("、") || "—" }} · {{ it.c.sentences }} 句 ·
        <RouterLink :to="'/c/' + it.c.id">{{ convLink(it.c) }}</RouterLink>
      </div>
      <div v-else-if="it.k === 'catchup'" class="gap">
        正在补齐 {{ it.device }} {{ hhmm(it.from) }}–{{ hhmm(it.to) }} 的录音{{
          it.eta ? `，约 ${Math.ceil(it.eta / 60)} 分钟` : ""
        }}
      </div>
      <div v-else class="gap">— {{ hhmm(it.at) }} —</div>
    </template>
    <div v-if="ready && !rows.length" class="empty">
      {{ live ? "今天还没有转写内容" : "这一天没有转写内容" }}
    </div>
  </main>
  <button v-show="live && !follow" class="jump" @click="toBottom">回到最新</button>
  <details
    class="syslog"
    :open="logOpen"
    @toggle="logOpen = ($event.target as HTMLDetailsElement).open"
  >
    <summary>
      系统日志 <span>{{ lastEvent }}</span>
    </summary>
    <div ref="logBox" class="events">
      <div v-for="(e, i) in events" :key="i" :class="e.kind">{{ hhmmss(e.at) }} {{ e.text }}</div>
    </div>
  </details>
</template>

<style scoped>
.status {
  font-size: 12px;
  color: var(--muted);
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
}
.live {
  padding-bottom: 120px;
}
.pane {
  flex-basis: 100%;
  border-top: 1px solid var(--line);
  margin-top: 4px;
}
.pane-body {
  overflow-y: auto;
  overscroll-behavior: contain;
}
.pane-meta {
  font-size: 12px;
  color: var(--muted);
  padding-top: 4px;
}
.grip {
  height: 14px;
  margin-bottom: -8px;
  cursor: ns-resize;
  touch-action: none;
  display: flex;
  align-items: center;
  justify-content: center;
}
.grip::after {
  content: "";
  width: 40px;
  height: 4px;
  border-radius: 2px;
  background: var(--line);
}
.grip:hover::after {
  background: var(--muted);
}
.conv {
  color: var(--accent);
  font-size: 12px;
  padding: 12px 0 4px;
  border-top: 2px solid var(--accent);
  margin-top: 8px;
}
.gap {
  color: var(--muted);
  font-size: 12px;
  text-align: center;
  padding: 10px 0 4px;
}
.head {
  flex: 1;
  min-width: 260px; /* below the nav on a phone outside the app */
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.back {
  margin-left: auto;
}
.jump {
  position: fixed;
  right: 24px;
  bottom: 64px;
  border-radius: 16px;
  padding: 6px 14px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}
.syslog {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  background: var(--bg);
  border-top: 1px solid var(--line);
  z-index: 2;
}
.syslog summary {
  max-width: 860px;
  margin: 0 auto;
  padding: 6px 16px;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
  list-style: none;
}
.syslog summary::before {
  content: "▸ ";
}
.syslog[open] summary::before {
  content: "▾ ";
}
.events {
  max-width: 860px;
  margin: 0 auto;
  padding: 0 16px 8px;
  max-height: 32vh;
  overflow: auto;
  font:
    12px/1.5 ui-monospace,
    monospace;
}
.events div {
  white-space: pre-wrap;
  word-break: break-all;
}
.events .state {
  color: var(--accent);
}
.events .warn {
  color: var(--warm);
}
.events .error {
  color: var(--err);
}
@media (max-width: 600px) {
  .status {
    flex-wrap: nowrap;
    overflow-x: auto;
    white-space: nowrap;
    gap: 12px;
    scrollbar-width: none;
  }
  .live {
    padding: 4px 12px 110px;
  }
}
</style>
