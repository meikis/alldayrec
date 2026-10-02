<script setup lang="ts">
import { onMounted, onUnmounted, ref, useTemplateRef, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import AppHeader from "../components/AppHeader.vue";
import DayPicker from "../components/DayPicker.vue";
import { get, hhmm, minutes, MOSTLY_MEDIA, type Conversation, type Status } from "../lib/api";
import { shiftDay, today } from "../lib/calendar";
import { useSocket } from "../lib/socket";

// newest first, PAGE at a time as the end scrolls into view; words, one tag and the kind filter it; ?day= starts
// the list at the end of that day (older ones keep coming as it scrolls)
const PAGE = 30;
const route = useRoute(),
  router = useRouter();
const q = ref(String(route.query.q ?? "")),
  tag = ref(String(route.query.tag ?? "")),
  kind = ref(String(route.query.kind ?? "对话")),
  day = ref(String(route.query.day ?? ""));
// the day of the topmost card on screen, shown by the picker
const seen = ref(""),
  find = useTemplateRef<HTMLElement>("find");
const tags = ref<[string, number][]>([]),
  items = ref<Conversation[]>([]),
  open = ref<Status["conversation"]>(null);
const loading = ref(false),
  done = ref(false);
const more = useTemplateRef<HTMLElement>("more");
let seq = 0;

const search = (before: string) =>
  get<Conversation[]>("/api/search", {
    q: q.value,
    tag: tag.value,
    kind: kind.value,
    before: before || (day.value ? shiftDay(day.value, 1) + "T00:00:00" : ""),
    limit: String(PAGE),
  });
async function restart() {
  // the old list stays on screen until the new first page is in: no blank flash
  router.replace({
    query: {
      ...(q.value && { q: q.value }),
      ...(tag.value && { tag: tag.value }),
      ...(kind.value !== "对话" && { kind: kind.value }),
      ...(day.value && { day: day.value }),
    },
  });
  const my = ++seq,
    plain = !q.value && !tag.value && kind.value === "对话" && !day.value;
  loading.value = true;
  const [st, cs] = await Promise.all([plain ? get<Status>("/api/status") : null, search("")]);
  if (my !== seq) return; // a newer search started meanwhile
  open.value = st?.conversation ?? null;
  items.value = cs;
  done.value = cs.length < PAGE;
  loading.value = false;
  spy();
}
async function page() {
  if (loading.value || done.value) return;
  const my = seq;
  loading.value = true;
  const cs = await search(items.value.at(-1)?.start ?? "");
  if (my !== seq) return;
  items.value.push(...cs);
  done.value = cs.length < PAGE;
  loading.value = false;
}
const dayOf = (i: number) => {
  const d = items.value[i].start.slice(0, 10);
  return i === 0 || items.value[i - 1].start.slice(0, 10) !== d ? d : "";
};

let timer = 0;
watch(q, () => {
  clearTimeout(timer);
  timer = window.setTimeout(restart, 300);
});
watch([tag, kind, day], restart);
watch(day, () => window.scrollTo(0, 0));
const pick = (d: string) => (day.value = d === today() ? "" : d);

function spy() {
  // the last day header above the bottom of the sticky search bar is the day being read
  const edge = (find.value?.getBoundingClientRect().bottom ?? 0) + 8;
  let d = items.value[0]?.start.slice(0, 10) ?? day.value;
  for (const el of document.querySelectorAll<HTMLElement>("[data-day]"))
    if (el.getBoundingClientRect().top < edge) d = el.dataset.day!;
  seen.value = d;
}

let io: IntersectionObserver;
onMounted(async () => {
  window.addEventListener("scroll", spy, { passive: true });
  io = new IntersectionObserver((es) => es[0].isIntersecting && page());
  if (more.value) io.observe(more.value);
  get<Record<string, number>>("/api/tags").then(
    (t) => (tags.value = Object.entries(t).slice(0, 40)),
  );
  await restart();
});
onUnmounted(() => {
  io?.disconnect();
  window.removeEventListener("scroll", spy);
});
useSocket((m) => {
  // summaries arrive every few seconds while old ones are redone: patch the card, never reload the list
  if (m.type === "summary") {
    const c = items.value.find((x) => x.id === m.conversation);
    if (c) c.summary = { ...m };
  } else if (
    m.type === "conversation" &&
    !q.value &&
    !tag.value &&
    !day.value &&
    window.scrollY < 200
  )
    restart();
}, restart);
</script>

<template>
  <AppHeader />
  <main>
    <div ref="find" class="find">
      <DayPicker :day="seen || day || today()" @pick="pick">
        <RouterLink class="cal" to="/calendar">日历</RouterLink>
      </DayPicker>
      <input v-model.trim="q" type="search" placeholder="搜索标题、摘要、待办、原文" />
      <div class="kinds">
        <button
          v-for="[k, label] in [
            ['对话', '对话'],
            ['口述', '口述'],
            ['', '全部'],
          ]"
          :key="k"
          :class="{ on: kind === k }"
          @click="kind = k"
        >
          {{ label }}
        </button>
      </div>
      <div class="tags">
        <button
          v-for="[t, n] in tags"
          :key="t"
          class="tag"
          :class="{ on: tag === t }"
          @click="tag = tag === t ? '' : t"
        >
          {{ t }} {{ n }}
        </button>
      </div>
    </div>
    <div v-if="open" class="card open">
      <div class="meta">{{ hhmm(open.start) }} 开始 · 进行中 · {{ open.sentences }} 句</div>
      <div class="title muted">对话进行中，结束后生成摘要</div>
    </div>
    <template v-for="(c, i) in items" :key="c.id">
      <div v-if="dayOf(i)" class="day" :data-day="dayOf(i)">{{ dayOf(i) }}</div>
      <RouterLink
        class="card"
        :class="{ media: (c.summary?.media_share ?? 0) >= MOSTLY_MEDIA }"
        :to="'/c/' + c.id"
      >
        <div class="meta">
          {{ (c.summary?.media_share ?? 0) >= MOSTLY_MEDIA ? "媒体 · " : ""
          }}{{ c.kind === "口述" ? "口述 · " : "" }}{{ hhmm(c.start) }}–{{ hhmm(c.end) }} ·
          {{ minutes(c) }} 分钟 · {{ (c.participants || []).join("、") || "—" }}
          <template v-if="c.summary?.todos?.length">
            · {{ c.summary.todos.length }} 条待办</template
          >
        </div>
        <div
          class="title"
          :class="{
            bad: c.summary?.status === 'failed',
            muted: c.summary?.status !== 'done' && c.summary?.status !== 'failed',
          }"
        >
          {{
            c.summary?.status === "done"
              ? c.summary.title || "（无标题）"
              : c.summary?.status === "failed"
                ? "摘要生成失败"
                : "摘要生成中…"
          }}
        </div>
        <template v-if="(c.summary?.media_share ?? 0) < MOSTLY_MEDIA">
          <div v-if="c.summary?.summary" class="gist">{{ c.summary.summary }}</div>
          <div v-if="c.summary?.tags?.length" class="tagline">
            <span v-for="t in c.summary.tags" :key="t" class="tag">{{ t }}</span>
          </div>
        </template>
      </RouterLink>
    </template>
    <div v-if="done && !items.length && !open" class="empty">
      {{ q || tag ? "没有匹配的对话" : "还没有对话" }}
    </div>
    <button v-show="!done" ref="more" class="more" @click="page">
      {{ loading ? "加载中…" : "更早" }}
    </button>
  </main>
</template>

<style scoped>
.find {
  position: sticky;
  top: 49px;
  background: var(--bg);
  padding: 10px 0 6px;
  z-index: 1;
}
.cal {
  margin-left: auto;
  font-size: 13px;
}
.find input {
  margin-top: 8px;
  width: 100%;
  font: inherit;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 7px 12px;
  outline: none;
}
.find input:focus {
  border-color: var(--accent);
}
.kinds {
  display: inline-flex;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  margin-top: 8px;
}
.kinds button {
  border: 0;
  border-radius: 0;
  padding: 3px 14px;
  color: #4a4f63;
}
.kinds button.on {
  background: var(--accent);
  color: #fff;
}
.tags {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: none;
  padding-top: 8px;
}
.day {
  font-size: 13px;
  color: var(--muted);
  margin: 20px 0 8px;
}
.card.media {
  opacity: 0.5;
}
.card {
  display: block;
  color: inherit;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 10px;
}
a.card:hover {
  border-color: var(--accent);
}
.card.open {
  border-style: dashed;
}
.meta {
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.title {
  font-weight: 600;
  margin: 2px 0;
}
.gist {
  font-size: 13px;
  color: #4a4f63;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.tagline {
  margin-top: 6px;
}
.tagline .tag {
  cursor: inherit;
  margin-right: 4px;
  padding: 0 8px;
}
.more {
  display: block;
  margin: 16px auto;
}
@media (max-width: 600px) {
  .find {
    top: 41px;
  }
}
/* after the phone rule: inside the app there is no header to stay under */
:global(.in-app .find) {
  top: 0;
}
</style>
