<script setup lang="ts">
import { ref, watch } from "vue";
import AppHeader from "../components/AppHeader.vue";
import { get, hhmm, type Person } from "../lib/api";

const props = defineProps<{ name: string }>();
const p = ref<Person | null>(null),
  missing = ref(false),
  shown = ref(20),
  allMine = ref(false);

watch(
  () => props.name,
  async () => {
    p.value = null;
    missing.value = false;
    try {
      p.value = await get<Person>(`/api/person/${encodeURIComponent(props.name)}`);
      document.title = props.name;
    } catch {
      missing.value = true;
    }
  },
  { immediate: true },
);
const day = (iso: string) => iso.slice(0, 10);
const link = (id: string, at: string | null) =>
  `/c/${id}` + (at ? `#${encodeURIComponent(at)}` : "");
</script>

<template>
  <AppHeader />
  <main>
    <RouterLink to="/speakers">← 说话人</RouterLink>
    <div v-if="missing" class="empty">没有带「{{ name }}」的对话</div>
    <template v-else-if="p">
      <h2>{{ p.name }}</h2>
      <div class="muted">
        {{ p.count }} 段对话 · {{ day(p.first) }} 到 {{ day(p.last) }}
        <template v-if="p.with.length">
          · 常一起：<template v-for="(w, i) in p.with" :key="w"
            >{{ i ? "、" : ""
            }}<RouterLink v-if="w !== '我'" :to="`/p/${encodeURIComponent(w)}`">{{ w }}</RouterLink
            ><template v-else>{{ w }}</template></template
          >
        </template>
      </div>
      <div v-if="p.tags.length" class="tags">
        <RouterLink
          v-for="t in p.tags"
          :key="t"
          class="tag"
          :to="{ path: '/conversations', query: { tag: t } }"
          >{{ t }}</RouterLink
        >
      </div>

      <template v-if="p.theirs.length">
        <h3>{{ p.name }} 的待办</h3>
        <ul class="items">
          <li v-for="(t, i) in p.theirs" :key="i">
            {{ t.text }}<span v-if="t.due" class="muted">（{{ t.due }}）</span>
            <RouterLink class="muted" :to="link(t.conversation, t.at)">{{
              t.title || "对话"
            }}</RouterLink>
          </li>
        </ul>
      </template>
      <template v-if="p.mine.length">
        <h3>我在和 {{ p.name }} 的对话里的待办</h3>
        <ul class="items">
          <li v-for="(t, i) in allMine ? p.mine : p.mine.slice(0, 8)" :key="i">
            {{ t.text }}<span v-if="t.due" class="muted">（{{ t.due }}）</span>
            <RouterLink class="muted" :to="link(t.conversation, t.at)">{{
              t.title || "对话"
            }}</RouterLink>
          </li>
        </ul>
        <button v-if="p.mine.length > 8 && !allMine" class="ghost" @click="allMine = true">
          全部 {{ p.mine.length }} 条
        </button>
      </template>

      <h3>对话</h3>
      <section v-for="c in p.conversations.slice(0, shown)" :key="c.id" class="conv">
        <RouterLink :to="`/c/${c.id}`" class="title">{{ c.title || "（无标题）" }}</RouterLink>
        <span class="muted">
          · {{ day(c.start) }} {{ hhmm(c.start) }} · {{ c.participants.join("、") }}</span
        >
        <ul v-if="c.points.length" class="points">
          <li v-for="x in c.points" :key="x">{{ x }}</li>
        </ul>
        <p v-else-if="c.summary" class="summary">{{ c.summary }}</p>
      </section>
      <button v-if="p.conversations.length > shown" class="more" @click="shown += 20">
        再显示 20 段
      </button>
    </template>
  </main>
</template>

<style scoped>
main {
  max-width: 860px;
  margin: 0 auto;
  padding: 12px 16px 48px;
}
h2 {
  margin: 12px 0 4px;
}
h3 {
  margin: 20px 0 6px;
  font-size: 15px;
}
.muted {
  color: var(--muted);
  font-size: 13px;
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.items {
  padding-left: 18px;
  margin: 0;
}
.items li {
  margin: 4px 0;
}
.items .muted {
  margin-left: 6px;
}
.conv {
  border-top: 1px solid var(--line);
  padding: 10px 0;
}
.conv .title {
  font-weight: 600;
}
.points {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 14px;
}
.summary {
  margin: 4px 0 0;
  font-size: 14px;
  color: var(--muted);
}
.more {
  width: 100%;
}
.empty {
  color: var(--muted);
  margin-top: 24px;
}
</style>
