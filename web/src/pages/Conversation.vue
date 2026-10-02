<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useRoute } from "vue-router";
import AppHeader from "../components/AppHeader.vue";
import MediaFold from "../components/MediaFold.vue";
import Timeline from "../components/Timeline.vue";
import TranscriptRow from "../components/TranscriptRow.vue";
import {
  fold,
  get,
  inSpans,
  hhmm,
  hhmmss,
  minutes,
  post,
  type Conversation,
  type Feedback,
  type LiveState,
  type Row,
} from "../lib/api";
import { useSocket } from "../lib/socket";

const props = defineProps<{ id: string }>();
const route = useRoute();
const c = ref<
    (Conversation & { rows: Row[]; timeline?: LiveState | null; feedback: Feedback[] }) | null
  >(null),
  missing = ref(false),
  hit = ref("");
const s = computed(() => c.value?.summary ?? null);
const segs = computed(() => fold(c.value?.rows ?? [], (r) => inSpans(r, s.value?.media_spans)));

async function load() {
  try {
    c.value = await get(`/api/conversation/${encodeURIComponent(props.id)}`);
  } catch {
    missing.value = true;
  }
}
function jumpTo(iso: string | null) {
  if (!iso) return;
  hit.value = iso;
  document.querySelector(`[data-start="${CSS.escape(iso)}"]`)?.scrollIntoView({ block: "center" });
}
watch(
  () => props.id,
  async () => {
    c.value = null;
    await load();
    await nextTick();
    if (route.hash) jumpTo(decodeURIComponent(route.hash.slice(1)));
  },
  { immediate: true },
);
useSocket((m) => {
  if (m.type === "summary" && m.conversation === props.id) load();
}, load);

async function regen() {
  await post(`/api/conversation/${props.id}/summarize`);
  await load();
}

// a correction in the owner's words, kept for a later read-back across many of them
const note = ref(""),
  sending = ref(false);
async function sendNote() {
  sending.value = true;
  try {
    c.value!.feedback = await post<Feedback[]>(`/api/conversation/${props.id}/feedback`, {
      text: note.value.trim(),
    });
    note.value = "";
  } finally {
    sending.value = false;
  }
}

// renaming one speaker of this conversation
const editing = ref(""),
  newName = ref(""),
  names = ref<string[]>([]),
  error = ref(""),
  flash = ref(""),
  saving = ref(false);
const named = (p: string) => p !== "我" && !/^S(\d+|\?)$/.test(p);
const labels = computed(() =>
  (c.value?.participants ?? []).filter((p) => p !== "我" && p !== "S?"),
);
async function edit(label: string) {
  editing.value = label;
  newName.value = "";
  error.value = "";
  names.value = (await get<string[]>("/api/names")).slice(0, 12);
}
async function save() {
  saving.value = true;
  try {
    const to = newName.value.trim();
    const r = await post<{ renamed: number; voiceprints: number }>(
      `/api/conversation/${props.id}/rename`,
      { from: editing.value, to },
    );
    flash.value =
      `${editing.value} → ${to}，改了 ${r.renamed} 句` +
      (r.voiceprints
        ? `，记住了 ${r.voiceprints} 句的声音，之后的对话会自动认出`
        : "（这段没有录音，不记声音）") +
      "；摘要重新生成中";
    editing.value = "";
    await load();
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <AppHeader />
  <main>
    <RouterLink to="/conversations">← 全部对话</RouterLink>
    <div v-if="missing" class="empty">找不到这段对话</div>
    <template v-else-if="c">
      <h2>{{ s?.status === "done" ? s.title || "（无标题）" : "对话 " + hhmm(c.start) }}</h2>
      <div class="muted">
        {{ c.start.slice(0, 10) }} {{ hhmm(c.start) }}–{{ hhmm(c.end) }} · {{ minutes(c) }} 分钟 ·
        {{ c.rows.length }} 句 ·
        <template v-for="(p, i) in c.participants" :key="p"
          >{{ i ? "、" : ""
          }}<RouterLink v-if="named(p)" :to="`/p/${encodeURIComponent(p)}`">{{ p }}</RouterLink
          ><template v-else>{{ p }}</template></template
        ><template v-if="!c.participants.length">—</template>
      </div>

      <div v-if="labels.length" class="rename">
        <template v-if="!editing">
          <span class="muted">改名：</span>
          <button v-for="l in labels" :key="l" class="tag" @click="edit(l)">{{ l }}</button>
        </template>
        <template v-else>
          <input
            v-model="newName"
            :placeholder="`${editing} 是谁`"
            autofocus
            @keydown.enter="save"
          />
          <button :disabled="saving || !newName.trim()" @click="save">保存</button>
          <button @click="editing = ''">取消</button>
          <span class="bad">{{ error }}</span>
          <button v-for="n in names" :key="n" class="tag" @click="newName = n">{{ n }}</button>
        </template>
      </div>
      <div v-if="flash" class="flash">{{ flash }}</div>

      <div v-if="s?.tags?.length" class="tags">
        <RouterLink
          v-for="t in s.tags"
          :key="t"
          class="tag"
          :to="{ path: '/conversations', query: { tag: t } }"
          >{{ t }}</RouterLink
        >
      </div>

      <template v-if="s?.status === 'done'">
        <div v-if="s.summary" class="summary">{{ s.summary }}</div>
        <template v-if="s.people?.length">
          <h3>各人要点</h3>
          <ul class="items">
            <li v-for="p in s.people" :key="p.who">
              <RouterLink v-if="named(p.who)" class="who" :to="`/p/${encodeURIComponent(p.who)}`">{{
                p.who
              }}</RouterLink>
              <span v-else class="who">{{ p.who }}</span>
              <ul class="points">
                <li v-for="x in p.points" :key="x">{{ x }}</li>
              </ul>
            </li>
          </ul>
        </template>
        <template v-if="s.todos?.length">
          <h3>待办</h3>
          <ul class="items">
            <li v-for="(t, i) in s.todos" :key="i">
              <span class="who">{{ t.owner || "未知" }}{{ t.to ? ` → ${t.to}` : "" }}</span
              >{{ t.text }}<span v-if="t.due" class="due">{{ t.due }}</span>
              <a v-if="t.at" class="at" :href="'#' + t.at" @click.prevent="jumpTo(t.at)">{{
                hhmmss(t.at)
              }}</a>
              <div v-if="t.quote" class="quote">「{{ t.quote }}」</div>
            </li>
          </ul>
        </template>
        <template v-if="s.ai_requests?.length">
          <h3>让 AI 做的事</h3>
          <ul class="items ai">
            <li v-for="(t, i) in s.ai_requests" :key="i">
              {{ t.text }}
              <a v-if="t.at" class="at" :href="'#' + t.at" @click.prevent="jumpTo(t.at)">{{
                hhmmss(t.at)
              }}</a>
            </li>
          </ul>
        </template>
        <template v-if="s.decisions?.length">
          <h3>决定</h3>
          <ul class="items">
            <li v-for="(d, i) in s.decisions" :key="i">
              {{ d.text
              }}<a v-if="d.at" class="at" :href="'#' + d.at" @click.prevent="jumpTo(d.at)">{{
                hhmmss(d.at)
              }}</a>
              <div v-if="d.quote" class="quote">「{{ d.quote }}」</div>
            </li>
          </ul>
        </template>
        <template v-if="s.topics?.length">
          <h3>议题</h3>
          <div v-for="(t, i) in s.topics" :key="i" class="topic">
            <b>{{ t.title }}</b
            ><a v-if="t.at" class="at" :href="'#' + t.at" @click.prevent="jumpTo(t.at)">{{
              hhmmss(t.at)
            }}</a>
            <ul>
              <li v-for="p in t.points" :key="p">{{ p }}</li>
            </ul>
          </div>
        </template>
        <div class="muted foot">
          {{
            s.model
              ? `由 ${s.model} 生成 · ${(s.created_at ?? "").slice(5, 16).replace("T", " ")}`
              : "语音不足 60 秒，没有生成摘要"
          }}
          <button @click="regen">重新生成</button>
        </div>
      </template>
      <div v-else-if="s?.status === 'failed'" class="summary bad">
        摘要生成失败：{{ s.error }} <button @click="regen">重新生成</button>
      </div>
      <div v-else class="summary muted">摘要生成中…</div>

      <div class="note">
        <textarea
          v-model="note"
          rows="2"
          placeholder="哪里判断错了？比如：这段不是视频、S2 其实是我、摘要漏了某件事"
        ></textarea>
        <button :disabled="sending || !note.trim()" @click="sendNote">提交纠正</button>
        <div v-for="(f, i) in c.feedback" :key="i" class="muted">
          {{ f.at.slice(5, 16).replace("T", " ") }} {{ f.text }}
        </div>
      </div>

      <template v-if="c.timeline?.topics.length">
        <h3>会中实时议题</h3>
        <Timeline :topics="c.timeline.topics" @jump="jumpTo" />
      </template>

      <h3>原文</h3>
      <template v-for="g in segs" :key="g.k === 'row' ? g.r.id : g.key">
        <TranscriptRow v-if="g.k === 'row'" :row="g.r" :media="g.media" :hit="hit === g.r.start" />
        <MediaFold v-else :rows="g.rows" :hit="hit" />
      </template>
    </template>
  </main>
</template>

<style scoped>
h2 {
  font-size: 20px;
  margin: 16px 0 4px;
}
h3 {
  font-size: 14px;
  color: var(--muted);
  font-weight: 600;
  margin: 22px 0 6px;
}
.tags {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: none;
  padding-top: 8px;
}
.summary {
  background: var(--soft);
  border-radius: 10px;
  padding: 12px 14px;
  margin-top: 12px;
}
ul.items {
  list-style: none;
  padding: 0;
  margin: 0;
}
ul.items > li {
  padding: 6px 0;
  border-bottom: 1px solid var(--line);
}
.points {
  margin: 2px 0 0;
  padding-left: 18px;
  font-size: 14px;
}
.who {
  color: var(--accent);
  font-size: 13px;
  margin-right: 6px;
}
.due {
  color: var(--warm);
  font-size: 12px;
  margin-left: 6px;
}
.quote {
  font-size: 12px;
  color: var(--muted);
}
.at {
  font-size: 12px;
  margin-left: 6px;
  font-variant-numeric: tabular-nums;
}
.topic ul {
  margin: 2px 0 0;
  padding-left: 18px;
  font-size: 14px;
}
.foot {
  margin-top: 12px;
}
.foot button {
  margin-left: 8px;
}
.rename {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  font-size: 13px;
}
.rename input {
  font: inherit;
  border: 1px solid var(--accent);
  border-radius: 8px;
  padding: 4px 10px;
  width: 9em;
  outline: none;
}
.flash {
  margin-top: 10px;
  font-size: 13px;
  color: var(--accent);
}
.note {
  margin-top: 12px;
  font-size: 13px;
}
.note textarea {
  font: inherit;
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 6px 10px;
  resize: vertical;
}
.note button {
  margin: 4px 0;
}
.items.ai li {
  color: var(--muted);
}
</style>
