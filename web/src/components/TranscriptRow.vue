<script setup lang="ts">
import { hhmmss, type Row } from "../lib/api";
import { player, toggle } from "../lib/audio";

defineProps<{ row: Row; hit?: boolean; media?: boolean }>(); // media: played aloud, not said in the room
</script>

<template>
  <div
    class="row"
    :class="{ pending: row.state && row.state !== 'final', hit, media }"
    :data-start="row.start"
  >
    <div class="t">
      <button
        class="play"
        :disabled="player.failed.has(row.id)"
        :title="player.failed.has(row.id) ? '这段录音取不出来' : '播放这一句'"
        @click="toggle(row)"
      >
        {{ player.failed.has(row.id) ? "×" : player.playing === row.id ? "■" : "▶" }}</button
      >{{ hhmmss(row.start) }}
    </div>
    <div
      class="spk"
      :class="{
        me: row.speaker === '我',
        other: row.speaker && row.speaker !== 'S?' && row.speaker !== '我',
      }"
      :title="row.speaker_confidence == null ? '' : `置信度 ${row.speaker_confidence}`"
    >
      {{ row.speaker || "—" }}
    </div>
    <div v-if="row.error" class="text err">识别失败：{{ row.error }}</div>
    <div v-else class="text">
      <span v-if="row.dictation" class="dict">口述→{{ row.dictation }}</span
      >{{ row.text || "（空）" }}
    </div>
  </div>
</template>

<style scoped>
.row {
  display: grid;
  grid-template-columns: 80px 48px 1fr;
  gap: 12px;
  padding: 6px 0;
  border-bottom: 1px solid var(--line);
}
.row.hit {
  background: #fff6d6;
}
.t,
.spk {
  color: var(--muted);
  font-size: 13px;
  padding-top: 1px;
  font-variant-numeric: tabular-nums;
}
.spk.me {
  color: var(--accent);
  font-weight: 600;
}
.spk.other {
  color: var(--warm);
}
.row.media {
  opacity: 0.45;
}
.row.pending .text,
.row.pending .spk {
  color: var(--muted);
}
.play {
  border: 0;
  background: none;
  color: var(--accent);
  cursor: pointer;
  padding: 0 4px 0 0;
  font-size: 12px;
  border-radius: 0;
}
.play:disabled {
  color: var(--muted);
  cursor: default;
}
.text {
  word-break: break-word;
}
.err {
  color: var(--err);
  font-size: 13px;
}
@media (max-width: 600px) {
  .row {
    grid-template-columns: 70px 30px 1fr;
    gap: 8px;
  }
  .t,
  .spk {
    font-size: 12px;
  }
  .play {
    padding: 0 2px 0 0;
  }
}
</style>
