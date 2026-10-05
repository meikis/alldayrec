"""Due todos and 完成 / 不对: the due list holds my dated todos of today and before, a todo marked either way leaves it
and the recap card."""
import asyncio
import datetime as dt
import tempfile
from pathlib import Path

import db
from server import add_feedback, recap_get, todos_due


def test_due_and_marks():
    db.open_db(Path(tempfile.mkdtemp()) / "t.db")
    today = dt.date.today()
    d = lambda n: (today + dt.timedelta(days=n)).isoformat()
    mine = lambda text, due_date: {"owner": "我", "text": text, "due": "某天", "due_date": due_date}
    with db.tx() as c:
        for cid, v, todos in [
                ("c20261001100000", 8, [mine("今天的", d(0)), mine("明天的", d(1)), mine("前天的", d(-2)), mine("没日期", None),
                                        {"owner": "S1", "text": "别人的", "due": "今天", "due_date": d(0)}, mine("做完了", d(0)), mine("记错了", d(-1))]),
                ("c20261001110000", 7, [mine("旧版", d(0))]),  # before v8 a due_date is not trusted (there was none)
                ("c20261001120000", 8, [mine("没有这段对话", d(0))])]:
            if cid != "c20261001120000":
                db.insert_conversation(c, dict(id=cid, start="2026-10-01T10:00:00.000", end="2026-10-01T10:30:00.000", participants=["我"], sentences=1))
            db.put_summary({"conversation": cid, "status": "done", "prompt_version": v, "title": "t", "todos": todos}, c)
    db.put_kv("recap:2026-10-01", {"date": "2026-10-01", "todos": [{"conv": "c20261001100000", "text": "做完了", "short": "做完"},
                                                                    {"conv": "c20261001100000", "text": "今天的"}]})
    asyncio.run(add_feedback("c20261001100000", {"text": db.TODO_MARKS[0] + "做完了"}))
    asyncio.run(add_feedback("c20261001100000", {"text": db.TODO_MARKS[1] + "记错了"}))
    asyncio.run(add_feedback("c20261001100000", {"text": "S2 其实是我"}))  # a plain correction closes nothing
    got = asyncio.run(todos_due())
    assert [t["text"] for t in got] == ["前天的", "今天的"], got
    assert got[1] == {"conv": "c20261001100000", "text": "今天的", "to": None, "due": "某天", "due_date": d(0), "at": None}
    assert [t["text"] for t in asyncio.run(recap_get())["todos"]] == ["今天的"]
    assert [t["text"] for t in asyncio.run(recap_get("2026-10-01"))["todos"]] == ["今天的"]
