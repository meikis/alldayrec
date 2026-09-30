import { reactive } from "vue";
import type { Row } from "./api";

// one clip plays at a time, on every page
export const player = reactive({ playing: "", failed: new Set<string>() });
let audio: HTMLAudioElement | null = null;

export function play(id: string, src: string) {
  audio?.pause();
  if (player.playing === id) {
    player.playing = "";
    return;
  }
  audio = new Audio(src);
  player.playing = id;
  audio.onended = () => (player.playing = "");
  audio.onerror = () => {
    player.failed.add(id);
    player.playing = "";
  };
  void audio.play();
}

export const toggle = (r: Row) =>
  play(r.id, `/api/audio?${new URLSearchParams({ start: r.start, end: r.end })}`);
