import { reactive } from "vue";
import type { Row } from "./api";

// one sentence plays at a time, on every page
export const player = reactive({ playing: "", failed: new Set<string>() });
let audio: HTMLAudioElement | null = null;

export function toggle(r: Row) {
  audio?.pause();
  if (player.playing === r.id) {
    player.playing = "";
    return;
  }
  audio = new Audio(`/api/audio?${new URLSearchParams({ start: r.start, end: r.end })}`);
  player.playing = r.id;
  audio.onended = () => (player.playing = "");
  audio.onerror = () => {
    player.failed.add(r.id);
    player.playing = "";
  };
  void audio.play();
}
