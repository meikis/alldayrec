// node --experimental-strip-types web/socket.check.ts
import assert from "node:assert/strict";
import { createRenderer, defineComponent, type Ref } from "vue";
import { useSocket } from "./src/lib/socket.ts";
import { useMutation } from "./src/lib/mutation.ts";

let now = 0;
let nextId = 0;
const timers = new Map<number, { at: number; interval: number; fn: () => void }>();
const schedule = (fn: () => void, delay: number, interval = 0) => {
  const id = ++nextId;
  timers.set(id, { at: now + delay, interval, fn });
  return id;
};
function advance(ms: number) {
  const end = now + ms;
  for (;;) {
    const due = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
    if (!due) break;
    const [id, t] = due;
    now = t.at;
    if (t.interval) t.at += t.interval;
    else timers.delete(id);
    t.fn();
  }
  now = end;
}
const visibility = new Set<() => void>();
class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  sent: string[] = [];
  constructor(_url: string) { Socket.instances.push(this); }
  open() { this.readyState = 1; this.onopen?.(); }
  close() { this.readyState = 3; this.onclose?.(); }
  send(data: string) { this.sent.push(data); }
}
const saved = Object.getOwnPropertyDescriptors(globalThis);
const originalNow = Date.now;
try {
  Object.assign(globalThis, {
    window: {
      setTimeout: (fn: () => void, ms: number) => schedule(fn, ms),
      setInterval: (fn: () => void, ms: number) => schedule(fn, ms, ms),
    },
    clearTimeout: (id: number) => timers.delete(id),
    clearInterval: (id: number) => timers.delete(id),
    location: { protocol: "https:", host: "example.test" },
    document: {
      hidden: false,
      addEventListener: (_: string, fn: () => void) => visibility.add(fn),
      removeEventListener: (_: string, fn: () => void) => visibility.delete(fn),
    },
    WebSocket: Socket,
  });
  Date.now = () => now;
  const renderer = createRenderer<object, object>({
    patchProp() {}, insert() {}, remove() {}, createElement: () => ({}),
    createText: () => ({}), createComment: () => ({}), setText() {}, setElementText() {},
    parentNode: () => null, nextSibling: () => null,
  });
  let socket!: { connected: Ref<boolean>; send: (message: unknown) => boolean };
  let resyncs = 0;
  let messages = 0;
  function mount() {
    const app = renderer.createApp(defineComponent({
      setup() {
        socket = useSocket(() => messages++, () => resyncs++);
        return () => null;
      },
    }));
    app.mount({});
    return app;
  }
  let app = mount();
  const first = Socket.instances[0]!;
  first.open();
  assert.equal(socket.connected.value, true);
  first.close();
  assert.equal(socket.connected.value, false);
  app.unmount();
  advance(20000);
  assert.equal(Socket.instances.length, 1, "unmount cancels a pending retry");
  assert.equal(timers.size, 0);
  assert.equal(visibility.size, 0);
  assert.equal(socket.send({ after: "unmount" }), false);

  app = mount();
  const second = Socket.instances[1]!;
  second.open();
  advance(14000);
  second.close();
  advance(1000); // watchdog runs while a retry is pending
  visibility.forEach((fn) => fn());
  assert.equal(Socket.instances.length, 2, "watchdog does not race the pending retry");
  advance(1000);
  const third = Socket.instances[2]!;
  third.open();
  assert.equal(resyncs, 1);
  assert.equal(socket.send({ hello: 1 }), true);
  assert.deepEqual(third.sent, ['{"hello":1}']);
  third.onmessage?.({ data: '{"type":"status"}' });
  assert.equal(messages, 1);
  advance(20000); // silent socket is replaced once by watchdog
  assert.equal(Socket.instances.length, 4);
  assert.equal(third.readyState, 3);
  assert.equal(third.onmessage, null);
  const fourth = Socket.instances[3]!;
  fourth.open();
  assert.equal(resyncs, 2);
  app.unmount();
  advance(20000);
  assert.equal(Socket.instances.length, 4);
  assert.equal(fourth.readyState, 3);
  assert.equal(socket.connected.value, false);
  assert.equal(timers.size, 0);
  assert.equal(visibility.size, 0);

  const mutation = useMutation();
  let finish!: () => void;
  let calls = 0;
  const pending = mutation.run(async () => {
    calls++;
    await new Promise<void>((resolve) => { finish = resolve; });
    throw new Error("request failed");
  });
  assert.equal(mutation.busy.value, true);
  await mutation.run(async () => { calls++; });
  assert.equal(calls, 1, "duplicate submissions are ignored while busy");
  finish();
  await pending;
  assert.equal(mutation.error.value, "request failed");
  assert.equal(mutation.busy.value, false);
  await mutation.run(async () => { calls++; });
  assert.equal(calls, 2);
  assert.equal(mutation.error.value, "");
  assert.equal(mutation.busy.value, false);
} finally {
  Date.now = originalNow;
  for (const key of ["window", "document", "location", "WebSocket", "clearTimeout", "clearInterval"]) {
    if (saved[key]) Object.defineProperty(globalThis, key, saved[key]!);
    else Reflect.deleteProperty(globalThis, key);
  }
}
console.log("socket lifecycle and mutation checks passed");
