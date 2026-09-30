<script setup lang="ts">
import { onMounted, onUnmounted, ref, useTemplateRef, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import AppHeader from "../components/AppHeader.vue";
import { get, hhmm, minutes, type Conversation, type Status } from "../lib/api";
import { useSocket } from "../lib/socket";

// newest first, PAGE at a time as the end scrolls into view; words, one tag and the kind filter it
const PAGE = 30;
const route = useRoute(),
  router = useRouter();
const q = ref(String(route.query.q ?? "")),
  tag = ref(String(route.query.tag ?? "")),
  kind = ref(String(route.query.kind ?? "对话"));
const tags = ref<[string, number][]>([]),
  items = ref<Conversation[]>([]),
  open = ref<Status["conversation"]>(null);
const loading = ref(false),
  done = ref(false);
const more = useTemplateRef<HTMLElement>("more");
let seq = 0;

async function restart() {
  router.replace({
    query: {
      ...(q.value && { q: q.value }),
      ...(tag.value && { tag: tag.value }),
      ...(kind.value !== "对话" && { kind: kind.value }),
    },
  });
  const my = ++seq;
  items.value = [];
  done.value = false;
  loading.value = false;
  open.value =
    !q.value && !tag.value && kind.value === "对话"
      ? ((await get<Status>("/api/status")).conversation ?? null)
      : null;
  if (my === seq) await page();
}
async function page() {
  if (loading.value || done.value) return;
  const my = seq,
    before = items.value.at(-1)?.start ?? "";
  loading.value = true;
  const cs = await get<Conversation[]>("/api/search", {
    q: q.value,
    tag: tag.value,
    kind: kind.value,
    before,
    limit: String(PAGE),
  });
  if (my !== seq) return; // a newer search started meanwhile
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
watch([tag, kind], restart);

let io: IntersectionObserver;
onMounted(async () => {
  io = new IntersectionObserver((es) => es[0].isIntersecting && page());
  if (more.value) io.observe(more.value);
  get<Record<string, number>>("/api/tags").then(
    (t) => (tags.value = Object.entries(t).slice(0, 40)),
  );
  await restart();
});
onUnmounted(() => io?.disconnect());
useSocket((m) => {
  if (
    (m.type === "summary" || m.type === "conversation") &&
    !q.value &&
    !tag.value &&
    window.scrollY < 200
  )
    restart();
}, restart);
</script>

<template>
  <AppHeader />
  <main>
    <div class="find">
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
      <div v-if="dayOf(i)" class="day">{{ dayOf(i) }}</div>
      <RouterLink class="card" :to="'/c/' + c.id">
        <div class="meta">
          {{ c.kind === "口述" ? "口述 · " : "" }}{{ hhmm(c.start) }}–{{ hhmm(c.end) }} ·
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
        <div v-if="c.summary?.summary" class="gist">{{ c.summary.summary }}</div>
        <div v-if="c.summary?.tags?.length" class="tagline">
          <span v-for="t in c.summary.tags" :key="t" class="tag">{{ t }}</span>
        </div>
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
:global(.in-app) .find {
  top: 0;
}
.find input {
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
</style>
