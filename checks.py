import importlib
import json
import sys
from pathlib import Path
from release import Publisher, Receiver, Stop


def sample():
    return json.loads(Path("samples.json").read_text())


def normal():
    data = sample()
    receiver = Receiver()
    p = Publisher(receiver)
    p.revise(data["source"])
    a, b = data["first"], data["overlap"]
    p.submit(a["job"], a["epoch"], a["members"])
    h = p.prepare(a["job"], "w1", 0, draw=lambda: 0)
    assert h["payload"] == {"epoch": "may", "count": 1, "size": 2}
    p.push(h)
    assert p.prepare(a["job"], "w2", 100) is None
    p.submit(b["job"], b["epoch"], b["members"])
    p.push(p.prepare(b["job"], "w2", 100, draw=lambda: 0))
    assert receiver.observed()[-1]["payload"]["count"] == 2
    assert [p.usage(x) for x in ("ada", "bo", "cy")] == [1, 1, 1]
    before = p.snapshot()
    try:
        p.revise([["ada", 2, 1], ["bo", 1, 0]])
    except ValueError as e:
        assert str(e) == "REVISION_CONFLICT"
    else:
        raise AssertionError("missing conflict")
    assert p.snapshot() == before
    assert p.submit(a["job"], a["epoch"], list(reversed(a["members"]))) == "done"
    p.submit("held", "jun", ["ada"])
    p.prepare("held", "w1", 0, ttl=3, draw=lambda: 0)
    try:
        p.prepare("held", "w2", 2)
    except ValueError as e:
        assert str(e) == "BUSY"
    else:
        raise AssertionError("missing lease")
    takeover = p.prepare("held", "w2", 3)
    p.push(takeover)
    p.submit("over-cap", "jul", ["ada"])
    before = p.snapshot()
    try:
        p.prepare("over-cap", "w3", 10)
    except ValueError as e:
        assert str(e) == "BUDGET"
    else:
        raise AssertionError("missing budget")
    assert p.snapshot() == before
    print("NORMAL_REGRESSION_OK")
    return receiver.observed()


def exposed():
    r = Receiver()
    p = Publisher(r, cap=1)
    p.revise([["ada", 1, 0]])
    p.submit("lost", "may", ["ada"])
    h = p.prepare("lost", "w1", 0, draw=lambda: 0)
    try:
        p.push(h, fail="after_receiver")
    except Stop as e:
        assert str(e) == "AFTER_RECEIVER"
    p.cancel("lost")
    result = {"observations": r.observed(), "charged_epochs": p.usage("ada")}
    return result


def review():
    normal()
    actual = importlib.import_module("review_evidence").replay()
    saved = json.loads(Path("evidence.json").read_text())
    assert actual == saved, "evidence is stale"
    assert isinstance(actual, list) and actual
    report = Path("REVIEW.md").read_text()
    for case in actual:
        assert isinstance(case, dict) and isinstance(case["id"], str)
        assert case["id"] in report, "unreferenced evidence"
        assert all(k in case for k in ("calls", "expected", "actual", "locations"))
        assert case["locations"]
    print("REVIEW_REPLAY_OK")
    print(json.dumps(actual, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    if mode == "normal":
        normal()
    elif mode == "reproduce":
        result = exposed()
        print(json.dumps(result, sort_keys=True))
        if result["observations"] and result["charged_epochs"] == 0:
            print("PUBLISHED_WITHOUT_BUDGET")
            raise SystemExit(1)
        raise AssertionError("expected incident absent")
    elif mode == "demo":
        print(json.dumps({"normal": normal(), "incident": exposed()}, sort_keys=True))
    elif mode == "review":
        review()
    else:
        raise ValueError("MODE")
