<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import AppHeader from "../components/AppHeader.vue";
import DayPicker from "../components/DayPicker.vue";
import TranscriptRow from "../components/TranscriptRow.vue";
import {
  get,
  hhmm,
  hhmmss,
  type Conversation,
  type LogEvent,
  type Row,
  type Status,
} from "../lib/api";
import { loadCalendar, today as todayOf } from "../lib/calendar";
import { useSocket } from "../lib/socket";

// One day at a time (?day=YYYY-MM-DD; none: today, which follows the live stream)
const GAP_MS = 5 * 60 * 1000; // a quiet stretch this long gets a mark, so conversations separate
const route = useRoute(),
  router = useRouter();
const today = todayOf();
const day = computed(() => (route.query.day as string | undefined) || today),
  live = computed(() => day.value === today);
const rows = ref<Row[]>([]),
  convs = ref<Conversation[]>([]),
  events = ref<LogEvent[]>([]),
  status = ref<Status | null>(null);
const ready = ref(false);
const follow = ref(true),
  logOpen = ref(false),
  logBox = ref<HTMLElement | null>(null);

type Item =
  | { k: "row"; key: string; r: Row }
  | { k: "conv"; key: string; c: Conversation }
  | { k: "gap"; key: string; at: string };
const timeline = computed(() => {
  const ev = [
    ...rows.value.map((r) => ({ s: r.start, it: { k: "row", key: "s" + r.id, r } as Item })),
    ...convs.value.map((c) => ({ s: c.end, it: { k: "conv", key: "c" + c.id, c } as Item })),
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
  return out;
});
const lastEvent = computed(() => {
  const e = events.value.at(-1);
  return e ? `· ${hhmmss(e.at)} ${e.text}`.slice(0, 90) : "";
});

function finalize(m: { from: string; to: string; rows: Row[] }) {
  rows.value = rows.value.filter((r) => !(m.from <= r.start && r.start < m.to)).concat(m.rows);
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
  const [rs, cs] = await Promise.all([
    get<Row[]>("/api/sentences", { date: d }),
    get<Conversation[]>("/api/conversations", { date: d }),
  ]);
  if (d !== day.value) return; // another day was picked meanwhile
  rows.value = rs;
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

const { connected } = useSocket((m) => {
  // rows and conversations of another day than the one shown are not for this page
  if (m.type === "final") {
    if (live.value) finalize(m);
  } else if (m.type === "conversation") {
    if (m.start.slice(0, 10) === day.value) addConv(m);
  } else if (m.type === "summary") {
    const c = convs.value.find((x) => x.id === m.conversation);
    if (c) c.summary = { ...m };
  } else if (m.type === "event") addEvent(m);
  else status.value = m;
}, loadDay);

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
  </AppHeader>
  <main class="live">
    <template v-for="it in timeline" :key="it.key">
      <TranscriptRow v-if="it.k === 'row'" :row="it.r" />
      <div v-else-if="it.k === 'conv'" class="conv">
        对话 {{ hhmmss(it.c.start) }}–{{ hhmmss(it.c.end) }} 结束 · 参与
        {{ it.c.participants.join("、") || "—" }} · {{ it.c.sentences }} 句 ·
        <RouterLink :to="'/c/' + it.c.id">{{ convLink(it.c) }}</RouterLink>
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
