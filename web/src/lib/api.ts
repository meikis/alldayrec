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
    quote?: string;
  })[];
  ai_requests?: (At & { text: string })[];
  ai_spans?: { from: string; to: string }[];
  decisions?: (At & { text: string; quote?: string })[];
  model?: string | null;
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
  summary?: Summary | null;
}

export interface DeviceStatus {
  device: string;
  audio_end: string | null;
  received_to: string | null;
  done_to: string | null;
  left_s: number;
  rate: number | null;
  eta_s: number | null;
  queued: number;
  latency_s: number | null;
  refine?: { at: string; ms: number; error?: string; backlog_s?: number } | null;
  conversation?: { start: string; sentences: number } | null;
}

export interface Status {
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
export interface Person {
  name: string;
  count: number;
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
