"""Finite public strategies for replaying release-candidate observations."""
import json
from pathlib import Path
from release import Publisher, Receiver


def run(spec, history, draw=None):
    nodes = spec["nodes"]
    for i, node in enumerate(nodes):
        links = [node["yes"], node["no"]] if node["op"] == "branch" else [node["next"]]
        if any(type(x) is not int or (x != -1 and not i < x < len(nodes)) for x in links):
            raise ValueError("NOT_A_DAG")
    r = Receiver()
    p = Publisher(r, cap=spec["cap"])
    handles, trace = {}, []
    cursor, seen = 0, 0
    while cursor != -1:
        node = nodes[cursor]
        op = node["op"]
        following = node.get("next", -1)
        result = None
        try:
            if op == "revise":
                p.revise(history[node["at"]])
                result = "source_applied"
            elif op == "submit":
                result = p.submit(node["job"], node["epoch"], node["members"])
            elif op == "prepare":
                h = p.prepare(node["job"], node["worker"], node["now"],
                              ttl=node.get("ttl", 10), draw=draw)
                handles[node["save"]] = h
                result = "done" if h is None else "ready"
            elif op == "push":
                p.push(handles[node["handle"]], fail=node.get("fail"))
                result = "acknowledged"
            elif op == "cancel":
                result = p.cancel(node["job"])
            elif op == "branch":
                answers = [x for x in r.observed() if x["job"] == node["job"]]
                if not answers:
                    raise ValueError("NO_OBSERVATION")
                result = answers[-1]["payload"]["count"] == node["equals"]
                following = node["yes"] if result else node["no"]
            else:
                raise ValueError("OP")
        except (ValueError, RuntimeError) as error:
            result = {"error": str(error)}
        fresh = r.observed()[seen:]
        seen += len(fresh)
        trace.append({"node": cursor, "result": result, "observations": fresh})
        cursor = following
    return trace


if __name__ == "__main__":
    spec = json.loads(Path("sample-policy.json").read_text())
    histories = json.loads(Path("paired-histories.json").read_text())
    print(json.dumps(run(spec, histories["left"]), sort_keys=True))
