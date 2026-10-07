export interface Row {
  id: string;
  start: string;
  end: string;
  speaker?: string;
  speaker_confidence?: number | null;
  text?: string;
  state?: string;
  error?: string;
  dictation?: string;
  source?: string; // the device that recorded it
}

export interface Span {
  from: string; // starts of the first and the last row, both included
  to: string;
}
export const inSpans = (r: Row, spans?: Span[]) =>
  !!spans?.some((x) => x.from <= r.start && r.start <= x.to);
export const MOSTLY_MEDIA = 0.8; // a conversation this much media is greyed as a whole

// Rows in order, a run of FOLD or more media rows folded into one line; a shorter run stays, greyed
export type Seg = { k: "row"; r: Row; media: boolean } | { k: "fold"; key: string; rows: Row[] };
const FOLD = 3;
export function fold(rows: Row[], media: (r: Row) => boolean): Seg[] {
  const out: Seg[] = [];
  let run: Row[] = [];
  const flush = () => {
    if (run.length >= FOLD) out.push({ k: "fold", key: "m" + run[0].id, rows: run });
    else out.push(...run.map((r) => ({ k: "row" as const, r, media: true })));
    run = [];
  };
  for (const r of rows) {
    if (media(r)) run.push(r);
    else {
      flush();
      out.push({ k: "row", r, media: false });
    }
  }
  flush();
  return out;
}

export interface Feedback {
  conv: string;
  at: string;
  text: string;
}
// 完成 / 不对 on a todo is a feedback line: the mark, then the todo's text (server db.TODO_MARKS)
export const TODO_MARK = { done: "待办完成：", wrong: "待办不对：" } as const;

export interface At {
  at: string | null;
}
export interface Summary {
  conversation: string;
  status: "pending" | "done" | "failed";
  error?: string | null;
  title?: string;
  summary?: string;
  people?: { who: string; points: string[] }[];
  tags?: string[];
  topics?: (At & { title: string; points?: string[] })[];
  todos?: (At & {
    owner?: string;
    to?: string | null;
    text: string;
    due?: string | null;
    due_date?: string | null; // the due on the calendar (YYYY-MM-DD), from summary prompt v8 on
    quote?: string;
  })[];
  ai_requests?: (At & { text: string })[];
  ai_spans?: Span[];
  media_spans?: Span[]; // media played aloud (video, TV, music): greyed and folded
  media_share?: number; // of the speech, the part in media_spans
  decisions?: (At & { text: string; quote?: string })[];
  model?: string | null;
  via?: "live"; // written by the conversation's live agent at its close
  created_at?: string;
}

export interface Conversation {
  id: string;
  start: string;
  end: string;
  state?: "open" | "closed";
  participants: string[];
  sentences: number;
  kind?: string;
  device?: string | null; // its rows' source; none: from before per-device pipelines
  summary?: Summary | null;
}

export interface LiveTopic {
  id?: number; // the agent's handle; timelines kept before it have none
  title: string;
  start: string; // the row it starts at
  points: string[];
  decisions: string[];
  todos: { who?: string; what: string }[];
  open_questions: string[];
}
// the live analysis of an open conversation; kept as the conversation's timeline once it closes
export interface LiveState {
  device: string;
  start: string;
  topics: LiveTopic[];
  at: string;
  upto: string;
  sentences: number;
}

export interface DeviceStatus {
  device: string;
  audio_end: string | null;
  received_to: string | null;
  done_to: string | null;
  left_s: number;
  rate: number | null;
  eta_s: number | null;
  hold?: string | null; // finals wait here for older audio the phone is still sending (#130)
  queued: number;
  latency_s: number | null;
  refine?: { at: string; ms: number; error?: string; backlog_s?: number } | null;
  conversation?: { start: string; sentences: number } | null;
  hw?: RecorderStatus | null; // a BLE recorder, as its phone last read it
  arrival?: { at: number; lag_s: number } | null; // its newest audio: when it came (unix ms), how long after its end
  sound?: { s: number; zero_s: number; full: number } | null; // its last 10 min of audio: s of it, s in zero runs, full-scale samples
}

export const CHARGE = { battery: "用电池", charging: "充电中", full: "已充满" };

export interface RecorderStatus {
  at: string;
  model?: string;
  fw?: string;
  battery?: number;
  charge?: keyof typeof CHARGE; // what it runs on (firmware lr_power.h); none from an older firmware
  store?: number; // 0 recording to the card; an errno otherwise: with total_kb 0 no card (-19, -116), -28 full, -16 lent
  free_kb?: number;
  total_kb?: number;
  backlog_kb?: number; // left on the card for the phone to read in the background (#130)
  backlog_eta_s?: number; // at the phone's own reading rate
}

// GET /api/device-log: a device's events and the status reports its phones sent, in a time range
export interface DeviceEvent {
  src: string; // "rec": a record of the recorder's own event log (lib/recorderLog.ts); else the app that wrote it
  seq: number;
  at: string | null; // null: the phone could not put it on the clock
  data: Record<string, unknown>;
}
export interface DeviceLog {
  events: DeviceEvent[];
  status: RecorderStatus[];
  audio: AudioMinute[];
}
// a minute of a device's audio, how it sounded and arrived (the server's audio.health() of each piece; no content)
export interface AudioMinute {
  t: string; // the minute
  s: number; // seconds of audio in it
  rms: number; // dBFS
  hum: number; // dBFS: mains hum, 50..250 Hz
  zero_s: number; // in runs of exact zeros >= 10 ms: the mic sent nothing
  full: number; // full-scale samples: clipping
  lag_s: number | null; // how long after its end it arrived (null: before 10-07)
}

// GET /api/upgrades: a device's firmware updates, an app's "dfu" events joined with the recorder's DFU and BOOT records
export interface Upgrade {
  at: string; // begun: the app's start, else the recorder's DFU
  app: boolean; // an app logged it (DfuLog); else only the recorder's records tell (lr_dfu.py, apps before 1.4.1)
  from: string | null; // the app's revisions, else the BOOT records' firmware hashes
  to: string | null;
  ms: number | null; // to done or fail, else to the recorder's next BOOT
  ok: boolean | null; // null: no end and no BOOT yet; with no app, back on the same firmware (why says so)
  why: string | null;
  via?: string | null; // the bootloader the recorder went to: ota (Bluetooth), usb (UF2, lr.sh flash)
  stage?: string; // where a failure was: start (before the bootloader), boot (in it), send (with pct)
  pct?: number;
  back_ms?: number; // the end to the app's first status of the recorder
}

export interface Status {
  balance?: Balance | null; // StepFun's prepaid account (#155)
  device?: string | null;
  behind_s?: number | null;
  queued?: number; // uploaded pieces the server has not fed yet
  latency_s?: number | null;
  refine?: { at: string; ms: number; error?: string } | null;
  conversation?: { start: string; sentences: number } | null;
  engine?: string;
  commit?: string;
  devices?: DeviceStatus[];
  summaries_pending?: number;
}

// GET /api/calls: the ASR and LLM calls since a time, by kind and engine (#155), and the StepFun balance
export interface CallSum {
  kind: string; // final, draft (ASR); summary, hints, profile, recap, live, ask (LLM)
  engine: string;
  n: number;
  failed: Record<string, number>; // by HTTP code, else exception class
  ms50: number | null; // of the answered ones
  ms90: number | null;
  sent_h: number; // audio hours sent (ASR)
  prompt_tokens: number;
  completion_tokens: number;
}
export interface Balance {
  at: string;
  cny: number;
  per_day: number | null; // spent a day over the last 7 days, top-ups left out
  days_left: number | null;
}
export interface Calls {
  calls: CallSum[];
  balance: Balance | null;
}

export interface LogEvent {
  at: string;
  text: string;
  kind: string;
}

export async function get<T>(path: string, params: Record<string, string> = {}): Promise<T> {
  const q = new URLSearchParams(params).toString();
  const r = await fetch(path + (q ? "?" + q : ""));
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

export async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail ?? `HTTP ${r.status}`);
  return j;
}

export const hhmm = (iso: string) => iso.slice(11, 16);
export const hhmmss = (iso: string) => iso.slice(11, 19);
export const minutes = (c: Conversation) =>
  Math.max(1, Math.round((+new Date(c.end) - +new Date(c.start)) / 60000));

export interface Voice {
  conv: string;
  label: string;
  start: string;
  n: number;
  speech_s: number;
  clip?: string | null;
  clip_start?: string | null;
  clip_end?: string | null;
  clip_text?: string | null;
  title?: string | null;
  points: string[];
  with: string[];
}
export interface VoiceSet {
  count: number;
  speech_s: number;
  last: string | null;
  samples: Voice[];
  co: string[];
  called: { name: string; convs: string[] }[]; // what its voices were called, aliases resolved
  members?: Voice[]; // only from /api/speakers/{id}
}
export interface Speaker extends VoiceSet {
  id: string;
  name: string | null;
  suggested: (VoiceSet & { id: string; score: number })[];
}

export interface Todo {
  owner?: string;
  to?: string | null;
  text: string;
  due?: string | null;
  quote?: string;
  at: string | null;
  conversation: string;
  title?: string | null;
}
// GET /api/conversation/{id}/speakers: suggestions for its labels; each is applied only by a rename the owner clicks
export interface NameHint {
  who: string; // the label it was called
  name: string; // as said
  canon: string; // the person it is an alias of, else the name
  kind: "addressed" | "self" | "referred";
  by: string; // who said it
  at: string;
  quote: string;
}
export interface LabelSuggestions {
  asked: boolean; // the hints were asked for this conversation
  names: NameHint[];
  merge: { from: string; to: string; score?: number; name?: string }[];
  like: { who: string; name: string; score: number }[]; // voice near a named speaker
  elsewhere: { who: string; name: string; convs: number }[]; // its voice was called so in other conversations
  clips: Record<string, { clip?: string | null; clip_start?: string | null; clip_end?: string | null }>;
}

// a profile line with the evidence it came from (a conversation, a todo in it, a row that mentions the person)
export interface ProfileLine {
  text: string;
  src: { id: string; conv: string; at: string | null; start: string; title?: string | null }[];
}
export interface Profile {
  at: string;
  model?: string | null;
  upto: string | null; // the newest conversation it saw
  inputs: { conversations: number; todos: number; mentions: number };
  who: string;
  facts: ProfileLine[];
  recent: ProfileLine[];
  mentions: ProfileLine[]; // what others said about them
  open: ProfileLine[];
}

export interface Person {
  name: string;
  count: number;
  aliases: string[];
  profile: Profile | null;
  profile_newer: number; // their conversations after the profile was made
  first: string;
  last: string;
  with: string[];
  tags: string[];
  theirs: Todo[];
  mine: Todo[];
  conversations: {
    id: string;
    start: string;
    end: string;
    title?: string | null;
    summary?: string | null;
    points: string[];
    participants: string[];
    speech_s?: number;
  }[];
}
