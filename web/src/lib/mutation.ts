import { ref } from "vue";

// Keep failed edits available for retry; callers only clear their inputs after success.
export function useMutation() {
  const busy = ref(false);
  const error = ref("");
  async function run(action: () => Promise<void>) {
    if (busy.value) return;
    busy.value = true;
    error.value = "";
    try {
      await action();
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e);
    } finally {
      busy.value = false;
    }
  }
  return { busy, error, run };
}
