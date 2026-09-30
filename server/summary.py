"""Summary of a closed conversation: one chat call returning title, summary, topics, todos and decisions.

    LLM_BASE_URL, LLM_API_KEY, LLM_MODEL   any OpenAI-compatible /chat/completions endpoint

A conversation with less than MIN_SPEECH_S of speech gets no call: its title is its first sentence.
"""
import datetime as dt
import json
import os
import urllib.request

MIN_SPEECH_S = 60
PROMPT_VERSION = 3  # 2: tags, 3: people
SYSTEM = """你在整理一段现场录音转写出来的对话。每行是「[时间] 说话人: 文本」。说话人标签来自声纹聚类：「我」是录音者本人，S1、S2… 是其他人，同一个人可能被拆成多个标签，S? 是太短没认出来的句子。转写有错别字和同音误识别，按意图理解。标「我(口述→某应用)」的行是我在对电脑语音输入（多数是给 AI 下指令），不是说给在场的人听的：写成我让 AI 做了什么，不要当成和别人的对话。
只输出一个 JSON 对象：
{"title": "不超过 20 字的标题",
 "summary": "3~6 句话的摘要，第一人称写「我」",
 "people": [{"who": "说话人标签，原样照抄", "points": ["这个人在这段里说了什么：观点、提供的信息、提出的问题、表的态，2~5 条"]}],
 "tags": ["2~5 个标签：这段对话和什么相关（项目、领域、活动、人际场合），优先沿用已有标签"],
 "topics": [{"title": "议题", "points": ["要点"], "at": "议题开始那一行的时间 HH:MM:SS"}],
 "todos": [{"owner": "我 | S1 | 未知", "text": "要做的事", "due": "原话里的时间，没有写 null", "quote": "依据的原句", "at": "原句那一行的时间 HH:MM:SS"}],
 "decisions": [{"text": "定下来的事", "quote": "依据的原句", "at": "原句那一行的时间 HH:MM:SS"}]}
people 每个说了实质内容的标签一项（「我」在最前，S? 和只有附和的不列），按这个人自己的话写，不要写成别人对他的评价。
没有就给空数组，不编造原文里没有的内容。"""


KIND_NOTE = {"口述": "\n这段几乎全是我对电脑的语音输入：summary 写成这段时间我在让 AI 做什么、推进到哪一步、卡在哪里；todos 只收我明确要自己做或要 AI 做的事。"}


def transcript(rows: list[dict]) -> str:
    who = lambda r: (r.get("speaker") or "S?") + (f'(口述→{r["dictation"]})' if r.get("dictation") else "")
    return "\n".join(f'[{r["start"][11:19]}] {who(r)}: {r["text"]}' for r in rows if r.get("text"))


def chat(text: str, known_tags: list[str], kind: str = "对话") -> tuple[dict, dict]:
    """(parsed result, raw response); raises on HTTP errors and on a reply that is not JSON."""
    system = SYSTEM + KIND_NOTE.get(kind, "") + ("\n已有标签：" + "、".join(known_tags) if known_tags else "")
    req = {"model": os.environ["LLM_MODEL"], "response_format": {"type": "json_object"},
           "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
    r = urllib.request.urlopen(urllib.request.Request(
        os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions", json.dumps(req).encode(),
        {"Authorization": f'Bearer {os.environ["LLM_API_KEY"]}', "Content-Type": "application/json"}), timeout=300)
    raw = json.load(r)
    return json.loads(raw["choices"][0]["message"]["content"]), raw


def anchor(res: dict, rows: list[dict]) -> dict:
    """The model cites HH:MM:SS; turn each into the start of the row it names (null when none matches), topics in time order."""
    starts = [r["start"] for r in rows]
    def at(hms):
        return next((s for s in starts if s[11:19] == hms), None) if isinstance(hms, str) else None
    for k in ("topics", "todos", "decisions"):
        res[k] = [{**x, "at": at(x.get("at"))} for x in res.get(k) or []]
    res["topics"].sort(key=lambda x: x["at"] or "~")
    return res


def summarize(conv: dict, rows: list[dict], known_tags: list[str] = ()) -> dict:
    base = {"conversation": conv["id"], "prompt_version": PROMPT_VERSION,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"), "error": None}
    if conv.get("speech_s", 0) < MIN_SPEECH_S or not rows:
        first = next((r["text"] for r in rows if r.get("text")), "")
        return {**base, "status": "done", "model": None, "title": first[:20], "summary": "", "people": [], "tags": [], "topics": [], "todos": [], "decisions": []}
    res, raw = chat(transcript(rows), list(known_tags), conv.get("kind", "对话"))
    res = anchor(res, rows)
    return {**base, "status": "done", "model": raw.get("model"), "usage": raw.get("usage"),
            **{k: res.get(k) or ("" if k in ("title", "summary") else []) for k in ("title", "summary", "people", "tags", "topics", "todos", "decisions")}}


if __name__ == "__main__":
    rows = [dict(start="2026-09-30T10:00:01.200", text="我们先定一下", speaker="我"), dict(start="2026-09-30T10:02:00.000", text="好", speaker="S1")]
    r = anchor({"topics": [{"title": "b", "at": "10:02:00"}, {"title": "a", "at": "10:00:01"}], "todos": [{"text": "x", "at": "9:99"}]}, rows)
    assert [t["title"] for t in r["topics"]] == ["a", "b"] and r["topics"][0]["at"] == rows[0]["start"]
    assert r["todos"][0]["at"] is None and r["decisions"] == []
    s = summarize({"id": "c1", "speech_s": 12}, rows)
    assert s["title"] == "我们先定一下" and s["model"] is None, s
    print("ok")
