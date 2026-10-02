"""Live analysis of the open conversation: a timeline of topics that grows while the meeting goes on.

Topics that ended are frozen; each update sends their short form, the previous analysis of the last ones, and the
sentences from where the last ones start, and replaces only the last ones. A topic stays open for one more update
after a newer one starts: a change of subject is often clear only a few sentences later.
"""
import datetime as dt
import json
import os
import urllib.request

from summary import transcript

TAIL_MAX_S = 15 * 60  # one long topic: only its last 15 min of sentences go out, with its previous analysis
OPEN = 2              # topics re-analyzed each update
SYSTEM = """你在实时整理一场正在进行的会议或对话，帮在场的我随时回顾刚才讲了什么。输入是现场录音的转写，每行「[时间] 说话人: 文本」。「我」是录音者本人，S1、S2… 是其他人（同一个人可能被拆成多个标签），S? 是太短没认出来的句子。转写有错别字和同音误识别，按意图理解。
「我」对电脑或手机上的 AI 助手（Claude、Codex、ChatGPT 等）做语音输入、下指令的话不是会议内容，整理时跳过：特征是祈使句布置任务、提到代码文件日志命令、一口气一长段没人接话。
输入分三部分：
- 已定议题：之前的议题，不要改，只当上下文
- 上一版：最近几个议题上一次的整理结果，可能不完整、边界可能不准
- 原文：从上一版第一个议题开始到现在的转写（太长时只给最近一段，前面的内容以上一版为准）
按时间顺序把原文整理成议题，覆盖上一版的议题和之后新出现的议题。议题是一件在讨论的事，话题明显换了才开新议题，不要一两句话就开一个；上一版的标题和要点没错就尽量沿用，让结果稳定。
只输出一个 JSON 对象：
{"topics": [{
  "title": "不超过 16 字",
  "start": "议题第一句的时间 HH:MM:SS，必须是原文里出现的时间",
  "points": ["谁提了什么、主张或信息，每条一句，2~4 条，进行中的议题可以少"],
  "decisions": ["已经定下来的事"],
  "todos": [{"who": "谁做，「我」或说话人标签或名字", "what": "做什么"}],
  "open_questions": ["提出来了但还没定的问题"]
}]}
没有的字段给空数组。只写原文里有的，不要编。"""


def tail_from(state: dict) -> str | None:
    """Where the re-analyzed part starts: the first open topic, or the beginning when there is none."""
    t = state["topics"][state["frozen"]:]
    return t[0]["start"] if t else None


def request(state: dict, rows: list[dict]) -> tuple[str, str]:
    """(user message, the start of the sentences it covers)."""
    frozen, prev = state["topics"][:state["frozen"]], state["topics"][state["frozen"]:]
    lo = tail_from(state) or rows[0]["start"]
    tail = [r for r in rows if r["start"] >= lo]
    cut = (dt.datetime.fromisoformat(tail[-1]["start"]) - dt.timedelta(seconds=TAIL_MAX_S)).isoformat(timespec="milliseconds") if tail else lo
    tail = [r for r in tail if r["start"] >= cut]
    short = [{"title": t["title"], "start": t["start"][11:19], "points": t["points"][:2], "decisions": t["decisions"]} for t in frozen]
    hms = lambda t: {**t, "start": t["start"][11:19]}
    text = (f"已定议题：{json.dumps(short, ensure_ascii=False)}\n"
            f"上一版：{json.dumps([hms(t) for t in prev], ensure_ascii=False)}\n"
            f"原文{'（只给最近 15 分钟）' if cut > lo else ''}：\n{transcript(tail)}")
    return text, lo


def merge(state: dict, topics: list[dict], rows: list[dict], lo: str) -> dict:
    """The model's topics replace the open ones; each start becomes the start of a row at or after `lo`;
    all but the last OPEN freeze."""
    starts = [r["start"] for r in rows if r["start"] >= lo]
    def at(hms) -> str:  # the first row at or after the cited time; a time it made up falls on the next real one
        hms = hms if isinstance(hms, str) and len(hms) == 8 else ""
        return next((s for s in starts if s[11:19] >= hms), starts[-1])
    new = []
    for t in topics:
        if not isinstance(t, dict) or not t.get("title"):
            continue
        new.append({"title": str(t["title"]), "start": at(t.get("start")),
                    **{k: [x for x in t.get(k) or [] if x] for k in ("points", "decisions", "todos", "open_questions")}})
    new.sort(key=lambda t: t["start"])
    if not new:
        return state
    new[0]["start"] = min(new[0]["start"], lo)  # the open part keeps its beginning
    topics = state["topics"][:state["frozen"]] + new
    return {**state, "topics": topics, "frozen": max(state["frozen"], len(topics) - OPEN)}


def analyze(state: dict, rows: list[dict]) -> dict:
    """state: {topics, frozen, ...}; rows: the open conversation's sentences so far. Raises on HTTP and JSON errors."""
    text, lo = request(state, rows)
    req = {"model": os.environ["LLM_MODEL"], "response_format": {"type": "json_object"},
           "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}]}
    r = urllib.request.urlopen(urllib.request.Request(
        os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions", json.dumps(req).encode(),
        {"Authorization": f'Bearer {os.environ["LLM_API_KEY"]}', "Content-Type": "application/json"}), timeout=120)
    raw = json.load(r)
    topics = json.loads(raw["choices"][0]["message"]["content"]).get("topics") or []
    return {**merge(state, topics, rows, lo), "model": raw.get("model"), "usage": raw.get("usage")}


if __name__ == "__main__":
    rows = [dict(start=f"2026-10-02T10:{m:02d}:00.000", text=f"句{m}", speaker="S1") for m in range(0, 40, 2)]
    s = {"topics": [], "frozen": 0}
    s = merge(s, [{"title": "A", "start": "10:00:00", "points": ["a"]}, {"title": "B", "start": "10:09:30"}], rows, rows[0]["start"])
    assert [t["start"][11:19] for t in s["topics"]] == ["10:00:00", "10:10:00"] and s["frozen"] == 0, s
    assert tail_from(s) == rows[0]["start"]
    s = merge(s, [{"title": "A", "start": "10:00:00"}, {"title": "B", "start": "10:10:00"}, {"title": "C", "start": "10:20:00"}], rows, tail_from(s))
    assert s["frozen"] == 1 and tail_from(s) == "2026-10-02T10:10:00.000", s
    text, lo = request(s, rows)
    assert "[10:22:00]" not in text and "[10:24:00]" in text and '"title": "A"' in text  # 10:10-10:38: the last 15 min
    s2 = merge(s, [{"title": "B'", "start": "10:12:00"}], rows, tail_from(s))
    assert [t["title"] for t in s2["topics"]] == ["A", "B'"] and s2["topics"][1]["start"] == lo  # the open part keeps its start
    assert merge(s, [{"title": ""}], rows, lo) is s
    long = [dict(start=(dt.datetime(2026, 10, 2, 9) + dt.timedelta(minutes=m)).isoformat(timespec="milliseconds"), text="x", speaker="我") for m in range(40)]
    text, _ = request({"topics": [], "frozen": 0}, long)
    assert "最近 15 分钟" in text and "[09:23:00]" not in text and "[09:24:00]" in text
    print("ok")
