<script setup lang="ts">
import { hhmmss, type LiveTopic } from "../lib/api";

// open: the last topic is still being discussed
defineProps<{ topics: LiveTopic[]; open?: boolean }>();
defineEmits<{ jump: [start: string] }>();
</script>

<template>
  <div
    v-for="(t, i) in topics"
    :key="t.id ?? i + t.start"
    class="topic"
    :class="{ now: open && i === topics.length - 1 }"
  >
    <div class="head">
      <a class="at" :href="'#' + t.start" @click.prevent="$emit('jump', t.start)">{{
        hhmmss(t.start)
      }}</a>
      <b>{{ t.title }}</b>
      <span v-if="open && i === topics.length - 1" class="badge">进行中</span>
    </div>
    <ul>
      <li v-for="p in t.points" :key="p">{{ p }}</li>
      <li v-for="d in t.decisions" :key="'d' + d" class="dec">定：{{ d }}</li>
      <li v-for="(x, j) in t.todos" :key="'t' + j" class="todo">
        待办：<span class="who">{{ x.who || "未知" }}</span> {{ x.what }}
      </li>
      <li v-for="q in t.open_questions" :key="'q' + q" class="q">待定：{{ q }}</li>
    </ul>
  </div>
</template>

<style scoped>
.topic {
  padding: 6px 0;
  border-bottom: 1px solid var(--line);
}
.topic.now b {
  color: var(--accent);
}
.head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.at {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.badge {
  font-size: 11px;
  color: var(--accent);
  border: 1px solid var(--accent);
  border-radius: 8px;
  padding: 0 6px;
}
ul {
  margin: 2px 0 0;
  padding-left: 18px;
  font-size: 14px;
}
.dec {
  color: var(--accent);
}
.todo .who {
  color: var(--warm);
}
.q {
  color: var(--muted);
}
</style>
