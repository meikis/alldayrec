<script setup lang="ts">
// 完成 / 不对 on one todo: a feedback line naming it (TODO_MARK, then its text); a marked todo leaves the due list
// and the recaps. Marked lines stay in the conversation's feedback for the read-back
import { ref } from "vue";
import { post, TODO_MARK, type Feedback } from "../lib/api";

const props = defineProps<{ conv: string; text: string }>();
const emit = defineEmits<{ marked: [feedback: Feedback[]] }>();
const busy = ref(false);
async function mark(m: keyof typeof TODO_MARK) {
  busy.value = true;
  try {
    emit(
      "marked",
      await post<Feedback[]>(`/api/conversation/${props.conv}/feedback`, {
        text: TODO_MARK[m] + props.text,
      }),
    );
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <span class="marks">
    <button :disabled="busy" @click="mark('done')">完成</button>
    <button :disabled="busy" @click="mark('wrong')">不对</button>
  </span>
</template>

<style scoped>
.marks {
  white-space: nowrap;
}
.marks button {
  font-size: 12px;
  padding: 0 6px;
  margin-left: 6px;
}
</style>
