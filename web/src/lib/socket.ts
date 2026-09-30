import { onMounted, onUnmounted, ref } from "vue";

// The server pushes status every 2 s. After a long screen-off Android freezes the WebView and the socket
// dies without onclose, so silence longer than STALE_MS counts as a drop. onResync runs after every
// reconnect: what was pushed while away has to be fetched again.
const STALE_MS = 15000;

export function useSocket(onMessage: (m: any) => void, onResync: () => void) {
  const connected = ref(false);
  let ws: WebSocket | null = null,
    last = 0,
    opened = false,
    stopped = false,
    timer = 0;

  function connect() {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    last = Date.now();
    ws.onopen = () => {
      connected.value = true;
      if (opened) onResync();
      opened = true;
    };
    ws.onmessage = (e) => {
      last = Date.now();
      onMessage(JSON.parse(e.data));
    };
    ws.onclose = () => {
      connected.value = false;
      if (!stopped) setTimeout(connect, 2000);
    };
  }
  function watchdog() {
    if (!ws || document.hidden || Date.now() - last < STALE_MS) return;
    ws.onclose = null;
    ws.close();
    connected.value = false;
    connect();
  }
  onMounted(() => {
    connect();
    timer = window.setInterval(watchdog, 5000);
    document.addEventListener("visibilitychange", watchdog);
  });
  onUnmounted(() => {
    stopped = true;
    clearInterval(timer);
    document.removeEventListener("visibilitychange", watchdog);
    if (ws) {
      ws.onclose = null;
      ws.close();
    }
  });
  return { connected };
}
