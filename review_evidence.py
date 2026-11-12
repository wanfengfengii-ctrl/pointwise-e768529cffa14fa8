"""Independent review evidence for the privacy count publisher.

Written by the review team.  It never mutates production tables and never
imports production internals beyond the documented entry points
(release.Publisher / release.Receiver / release.Stop and policy.run).

Two independent engines are provided:

* ``compare(spec, left, right)`` computes the *exact* worst-case transcript
  probability ratio between two neighbouring person-histories for any finite
  public strategy.  Probabilities are rationals built from integer weights of
  the uniform 4-valued coins; the complete random tape is never enumerated.
  Shared coins (noise.py:15-25, release.py:63-64), cached payloads
  (release.py:137-142), repeated receiver observations (release.py:26-38) and
  adaptive branch nodes are modelled explicitly.  The convolution over
  independent per-person draw vectors exploits conditional independence; only
  the reachable count vectors are retained at each step.

* ``replay()`` drives the unmodified production entry points through isolated
  and combined failure scenarios and records independently computed
  expectations next to freshly observed behaviour.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from policy import run as policy_run
from release import Publisher, Receiver, Stop


class _Undecided(Exception):
    """Raised inside the simulator when a coin key has not been assigned yet."""


# ---------------------------------------------------------------------------
# Deterministic symbolic execution of one feasible DAG walk
# ---------------------------------------------------------------------------

def _enumerate_paths(nodes):
    """All root-to-termination node-id walks of the forward DAG."""
    paths = []

    def walk(cursor, acc):
        if cursor == -1:
            paths.append(acc)
            return
        node = nodes[cursor]
        if node["op"] == "branch":
            walk(node["yes"], acc + [cursor])
            walk(node["no"], acc + [cursor])
        else:
            walk(node.get("next", -1), acc + [cursor])

    walk(0, [])
    return paths


def _simulate_path(spec, history, path, coins):
    """Symbolically replay one path under a fixed (epoch,person)->draw table.

    Returns the exact trace shape ``policy.run`` produces, or ``None`` when
    the walk is infeasible (a branch would take the other edge, or branches on
    a job with no observation).  Mirrors release.py semantics without touching
    any production database.
    """
    nodes = spec["nodes"]
    source = {}                 # person -> (rev, bit)
    jobs = {}                   # id -> job record
    reservations = set()        # (job, person)
    handles = {}                # save-name -> handle dict or None
    observations = []           # append-only receiver observations
    trace = []
    cap = spec["cap"]

    def usage(person):
        # release.usage: count(DISTINCT epoch) over live reservations
        return len({jobs[j]["epoch"] for (j, p) in reservations if p == person})

    for step, node_id in enumerate(path):
        node = nodes[node_id]
        op = node["op"]
        seen = len(observations)
        result = None
        try:
            if op == "revise":
                for person, rev, bit in history[node["at"]]:
                    old = source.get(person)
                    if old and rev == old[0] and bit != old[1]:
                        raise ValueError("REVISION_CONFLICT")
                    if old is None or rev > old[0]:
                        source[person] = (rev, bit)
                result = "source_applied"
            elif op == "submit":
                job, epoch = node["job"], node["epoch"]
                members = sorted(node["members"])
                request = json.dumps([epoch, members], sort_keys=True,
                                     separators=(",", ":"))
                if job in jobs:
                    if jobs[job]["request"] != request:
                        raise ValueError("JOB_CONFLICT")
                    result = jobs[job]["state"]
                else:
                    if any(p not in source for p in members):
                        raise ValueError("UNKNOWN_PERSON")
                    jobs[job] = {"request": request, "epoch": epoch,
                                 "members": members, "state": "queued",
                                 "owner": None, "until": None, "payload": None}
                    result = "queued"
            elif op == "prepare":
                job, worker, now = node["job"], node["worker"], node["now"]
                ttl = node.get("ttl", 10)
                # policy.run assigns the save slot even when the call raises,
                # mirroring that the harness stores the (None) result.
                if node["save"] not in handles:
                    handles[node["save"]] = None
                if job not in jobs:
                    raise ValueError("UNKNOWN_JOB")
                j = jobs[job]
                if j["state"] == "cancelled":
                    raise ValueError("CANCELLED")
                if j["state"] == "done":
                    handles[node["save"]] = None
                    result = "done"
                else:
                    if (j["owner"] is not None and j["owner"] != worker
                            and now < j["until"]):
                        raise ValueError("BUSY")
                    ids, epoch = j["members"], j["epoch"]
                    # admission gate, release.py:129-134
                    live_jobs = sorted({j2 for (j2, _p) in reservations})
                    for person in ids:
                        reused = any(
                            (job2, person) in reservations
                            and jobs[job2]["epoch"] == epoch
                            for job2 in live_jobs)
                        if not reused and usage(person) >= cap:
                            raise ValueError("BUDGET")
                    for person in ids:              # release.py:135-136
                        reservations.add((job, person))
                    if j["payload"] is None:        # release.py:137-138
                        total = 0
                        for person in ids:          # noise.py:28-34
                            bit = source[person][1]
                            key = (epoch, person)
                            if key not in coins:
                                # Person not convolved yet: this walk cannot be
                                # judged from the current partial assignment.
                                raise _Undecided()
                            value = coins[key]
                            total += bit if value < 3 else 1 - bit
                        j["payload"] = {"epoch": epoch, "count": total,
                                        "size": len(ids)}
                    j["state"], j["owner"], j["until"] = (
                        "ready", worker, now + ttl)
                    handles[node["save"]] = {
                        "job": job, "worker": worker,
                        "payload": copy.deepcopy(j["payload"])}
                    result = "ready"
            elif op == "push":
                if node["handle"] not in handles:
                    # A dangling handle crashes the real harness (KeyError is
                    # outside its caught set); such specs are unsupported input.
                    raise ValueError("HANDLE")
                handle = handles[node["handle"]]
                if handle is None:
                    result = None
                else:
                    job = handle["job"]
                    body = json.dumps(handle["payload"], sort_keys=True,
                                      separators=(",", ":"))
                    prior = [o for o in observations if o["job"] == job]
                    if prior and json.dumps(prior[-1]["payload"],
                                            sort_keys=True,
                                            separators=(",", ":")) != body:
                        raise ValueError("RECEIVER_CONFLICT")
                    observations.append(
                        {"job": job, "payload": copy.deepcopy(handle["payload"])})
                    if node.get("fail") == "after_receiver":
                        raise RuntimeError("AFTER_RECEIVER")
                    j = jobs[job]
                    if j["state"] != "ready" or j["owner"] != handle["worker"]:
                        raise ValueError("STALE")
                    if json.dumps(j["payload"], sort_keys=True,
                                  separators=(",", ":")) != body:
                        raise ValueError("PAYLOAD")
                    j["state"] = "done"
                    result = "acknowledged"
            elif op == "cancel":
                job = node["job"]
                if job not in jobs:
                    raise ValueError("UNKNOWN_JOB")
                if jobs[job]["state"] == "done":
                    result = "done"
                else:
                    jobs[job]["state"] = "cancelled"
                    reservations = {r for r in reservations if r[0] != job}
                    result = "cancelled"
            elif op == "branch":
                answers = [o for o in observations if o["job"] == node["job"]]
                if not answers:
                    return None
                outcome = answers[-1]["payload"]["count"] == node["equals"]
                result = outcome
                actual_next = path[step + 1] if step + 1 < len(path) else -1
                if actual_next != (node["yes"] if outcome else node["no"]):
                    return None
        except (ValueError, RuntimeError) as error:
            result = {"error": str(error)}
        fresh = [{"seq": seen + i + 1, "job": o["job"],
                  "payload": copy.deepcopy(o["payload"])}
                 for i, o in enumerate(observations[seen:])]
        trace.append({"node": node_id, "result": result,
                      "observations": fresh})
    return trace


# ---------------------------------------------------------------------------
# Exact distribution via per-person factorized dynamic programming
# ---------------------------------------------------------------------------

def _coin_keys(spec, history):
    """Coin keys that can actually be created, intersected with admission.

    A prepare rejected by BUDGET/CANCELLED/BUSY never calls ``noise.coin``, so
    its nominal keys are not random variables. The simulator decides this per
    walk; the universe is the union of keys created on at least one feasible
    walk. People never appearing in any source batch are excluded as well.
    """
    people = set()
    for batch in history.values():
        for person, _rev, _bit in batch:
            people.add(person)
    paths = _enumerate_paths(spec["nodes"])
    created = set()
    for path in paths:
        created.update(_collect_created_keys(spec, history, path))
    return sorted(created), sorted(people)


def _collect_created_keys(spec, history, path):
    """Replay a walk with every coin set to 0, recording keys actually created.

    Deterministic admission/error outcomes do not depend on coin values except
    through branch edges; using all-zero coins and accepting either branch
    outcome still visits every prepare node on the walk, which is enough to
    learn which keys admission allows to be created.
    """
    nodes = spec["nodes"]
    source = {}
    jobs = {}
    reservations = set()
    created = []
    cap = spec["cap"]

    def usage(person):
        return len({jobs[j]["epoch"] for (j, p) in reservations if p == person})

    for node_id in path:
        node = nodes[node_id]
        op = node["op"]
        if op == "revise":
            for person, rev, bit in history[node["at"]]:
                old = source.get(person)
                if old and rev == old[0] and bit != old[1]:
                    break
                if old is None or rev > old[0]:
                    source[person] = (rev, bit)
        elif op == "submit":
            job, epoch = node["job"], node["epoch"]
            members = sorted(node["members"])
            if job not in jobs and all(p in source for p in members):
                jobs[job] = {"epoch": epoch, "members": members,
                             "state": "queued", "owner": None,
                             "until": None, "payload": None}
        elif op == "prepare":
            job, worker, now = node["job"], node["worker"], node["now"]
            ttl = node.get("ttl", 10)
            if job not in jobs:
                continue
            j = jobs[job]
            if j["state"] in ("cancelled", "done"):
                continue
            if (j["owner"] is not None and j["owner"] != worker
                    and now < j["until"]):
                continue
            ids, epoch = j["members"], j["epoch"]
            live_jobs = sorted({jj for (jj, _p) in reservations})
            denied = False
            for person in ids:
                reused = any((j2, person) in reservations
                             and jobs[j2]["epoch"] == epoch for j2 in live_jobs)
                if not reused and usage(person) >= cap:
                    denied = True
                    break
            if denied:
                continue
            for person in ids:
                reservations.add((job, person))
            if j["payload"] is None:
                for person in ids:
                    key = (epoch, person)
                    if key not in created:
                        created.append(key)
                j["payload"] = {"epoch": epoch, "count": 0, "size": len(ids)}
            j["state"], j["owner"], j["until"] = "ready", worker, now + ttl
        elif op == "push":
            # Payload contents and branch outcomes are irrelevant to key
            # discovery; only admission-affecting state transitions matter.
            pass
        elif op == "cancel":
            job = node["job"]
            if job in jobs and jobs[job]["state"] != "done":
                jobs[job]["state"] = "cancelled"
                reservations = {r for r in reservations if r[0] != job}
    return created


def _trace_key(trace):
    return json.dumps(trace, sort_keys=True, separators=(",", ":"))


def distribution(spec, history):
    """Exact distribution over complete public transcripts.

    The coins of distinct (epoch, person) keys are independent uniform values
    in range(4).  For each deterministic DAG walk the simulator decides
    feasibility from the branch edges, so the computation factorizes person
    by person: at every step only each person's local 4**k draw *vector* is
    enumerated and convolved into a weight map.  The complete joint random
    tape and every private database are never materialized.  Each full coin
    assignment satisfies exactly one walk, so the retained weights sum to the
    full sample space 4**(number of coin keys).
    """
    paths = _enumerate_paths(spec["nodes"])
    keys, people = _coin_keys(spec, history)
    by_person = {p: [k for k in keys if k[1] == p] for p in people}

    # Lanes are surviving partial coin assignments with their integer weights.
    # Invariant after processing t persons: the lane weights sum to
    # 4**(number of keys belonging to those persons). Lanes are never merged on
    # a partial trace, because persons processed later can split equal
    # prefixes; a lane is dropped only when a decided coin already contradicts
    # every walk's branch edges.
    lanes = [{"weight": 1, "table": {}}]
    for person in people:
        pkeys = by_person[person]
        vectors = _local_vectors(len(pkeys))
        advanced = []
        for lane in lanes:
            for vector in vectors:
                table = dict(lane["table"])
                table.update(zip(pkeys, vector))
                if _feasible_on_some_walk(spec, history, paths, table):
                    advanced.append({"weight": lane["weight"], "table": table})
        lanes = advanced
        expected_weight = 4 ** sum(
            len(by_person[p]) for p in people[:people.index(person) + 1])
        assert sum(lane["weight"] for lane in lanes) == expected_weight, \
            "per-person convolution must preserve total coin weight"

    total_weight = 4 ** len(keys)
    assert sum(lane["weight"] for lane in lanes) == total_weight, \
        "coin weights must normalize"

    dist = {}
    for lane in lanes:
        trace = None
        for path in paths:
            trace = _simulate_path(spec, history, path, lane["table"])
            if trace is not None:
                break
        assert trace is not None
        k = _trace_key(trace)
        if k in dist:
            dist[k]["weight"] += lane["weight"]
        else:
            dist[k] = {"weight": lane["weight"], "trace": trace,
                       "table": lane["table"]}

    out = {}
    for k, item in dist.items():
        frac = Fraction(item["weight"], total_weight)
        out[k] = {"num": frac.numerator, "den": frac.denominator,
                  "weight": item["weight"], "trace": item["trace"],
                  "table": item["table"]}
    return out


def _feasible_on_some_walk(spec, history, paths, table):
    """Whether a partial coin assignment can still lead to a terminal trace.

    A lane is pruned only when a *decided* coin already forces every walk to
    violate a branch edge. Missing coins raise ``_Undecided`` and keep the
    lane alive, since an unprocessed person may resolve either way.
    """
    for path in paths:
        try:
            if _simulate_path(spec, history, path, table) is not None:
                return True
        except _Undecided:
            return True
    return False


def _local_vectors(k):
    """All length-k vectors over range(4) for one person's coin keys."""
    if k == 0:
        return [()]
    out = []

    def build(i, acc):
        if i == k:
            out.append(tuple(acc))
            return
        for value in range(4):
            acc.append(value)
            build(i + 1, acc)
            acc.pop()

    build(0, [])
    return out


# ---------------------------------------------------------------------------
# Witness materialization through the real, unmodified policy.run entry point
# ---------------------------------------------------------------------------

def _shadow_creation_order(spec, history, walk_nodes=None):
    """Capture the (epoch, person) first-coin creation order of production.

    When ``walk_nodes`` is given, branch nodes are forced along that walk so
    the shadow reaches the same prepares as the target witness.
    """
    import noise
    real_coin = noise.coin
    order = []

    def spy_coin(db, epoch, person, draw=None):
        row = db.execute(
            "SELECT draw FROM coins WHERE epoch=? AND person=?",
            (epoch, person)).fetchone()
        if row is None:
            order.append((epoch, person))
        return real_coin(db, epoch, person, draw)

    noise.coin = spy_coin
    try:
        if walk_nodes is None:
            policy_run(spec, history, draw=lambda: 0)
        else:
            _forced_walk_run(spec, history, walk_nodes, lambda: 0)
    finally:
        noise.coin = real_coin
    return order


def materialize(spec, history, table, walk_nodes=None):
    """Run the real harness with a draw hook fed from a precomputed table.

    With ``walk_nodes`` the branch edges of the target witness are forced, so
    its exact walk is replayed through production code; leftover table entries
    belong to walks not taken and are simply unused. Without it the harness's
    natural branch outcomes are used, which only reproduces a witness whose
    counts already drive those edges.
    """
    order = _shadow_creation_order(spec, history, walk_nodes)
    values = [table[k] for k in order]
    cursor = {"i": 0}

    def draw():
        value = values[cursor["i"]]
        cursor["i"] += 1
        return value

    if walk_nodes is None:
        trace = policy_run(spec, history, draw=draw)
    else:
        trace = _forced_walk_run(spec, history, walk_nodes, draw)
    assert cursor["i"] == len(values), "coin creation order changed"
    return trace


def materialize_trace(spec, history, table, trace):
    """Replay a specific symbolic trace's walk through the real entry points."""
    return materialize(spec, history, table,
                       walk_nodes=[t["node"] for t in trace])


def _forced_walk_run(spec, history, walk_nodes, draw):
    """policy.run clone that forces branch edges to follow walk_nodes."""
    nodes = spec["nodes"]
    forced = set(walk_nodes)
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
                p.revise(history[node["at"]]); result = "source_applied"
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
                result = answers[-1]["payload"]["count"] == node["equals"]
                pos = walk_nodes.index(cursor)
                following = walk_nodes[pos + 1] if pos + 1 < len(walk_nodes) else -1
        except (ValueError, RuntimeError) as error:
            result = {"error": str(error)}
        fresh = r.observed()[seen:]
        seen += len(fresh)
        trace.append({"node": cursor, "result": result, "observations": fresh})
        cursor = following
    return trace


def compare(spec, left, right):
    """Tight maximum Pr_left(T)/Pr_right(T) over complete finite transcripts.

    ``ratio`` is a reduced [numerator, denominator] pair, or the string
    "infinity" together with a positive-left/zero-right witness.  Transcripts
    with zero probability under both histories are irrelevant.  The finite
    transcript maximum equals the maximum over all measurable transcript
    events because every transcript here is an atomic cylinder event whose
    internal prefixes carry no additional branching randomness.
    """
    dl = distribution(spec, left)
    dr = distribution(spec, right)
    # Both neighbouring histories share public metadata, hence the same set of
    # possible coin keys; each key's draw is uniform on range(4).
    n_left = 4 ** len(_coin_keys(spec, left)[0])
    n_right = 4 ** len(_coin_keys(spec, right)[0])
    assert n_left == n_right, "neighbours must share the public strategy"
    # Canonical (sorted) order so the selected infinity witness is stable.
    keys = sorted(set(dl) | set(dr))

    infinity_key = None
    candidates = []
    for k in keys:
        wl = dl[k]["weight"] if k in dl else 0
        wr = dr[k]["weight"] if k in dr else 0
        if wl == 0 and wr == 0:
            continue
        if wr == 0:
            infinity_key = k
            break
        candidates.append((Fraction(wl, wr), k))

    if infinity_key is not None:
        entry = dl[infinity_key]
        walk = [t["node"] for t in entry["trace"]]
        witness = materialize(spec, left, entry["table"], walk_nodes=walk)
        assert witness == entry["trace"], "symbolic/real trace divergence"
        pl = Fraction(entry["weight"], n_left)
        return {"ratio": "infinity", "witness": witness,
                "left_probability": [pl.numerator, pl.denominator],
                "right_probability": [0, 1]}

    ratio, k = max(candidates, key=lambda x: x[0])
    walk = [t["node"] for t in dl[k]["trace"]]
    witness = materialize(spec, left, dl[k]["table"], walk_nodes=walk)
    assert witness == dl[k]["trace"]
    pl = Fraction(dl[k]["weight"], n_left)
    pr = Fraction(dr[k]["weight"], n_right)
    return {"ratio": [ratio.numerator, ratio.denominator],
            "witness": witness,
            "left_probability": [pl.numerator, pl.denominator],
            "right_probability": [pr.numerator, pr.denominator]}


# ---------------------------------------------------------------------------
# Production-entry-point evidence suite
# ---------------------------------------------------------------------------

L = {
    "coin_memo": "noise.py:15-25",
    "count_fn": "noise.py:28-34",
    "coins_ddl": "release.py:63-64",
    "reservations_ddl": "release.py:61-62",
    "admission": "release.py:129-136",
    "prepare_payload": "release.py:137-142",
    "push": "release.py:144-157",
    "receiver_emit": "release.py:26-38",
    "cancel": "release.py:159-168",
    "usage": "release.py:107-110",
    "tx": "release.py:67-75",
    "revise": "release.py:77-87",
}


def _rr(bit, draw):
    """Independent expectation of one memoized randomized-response output."""
    return bit if draw < 3 else 1 - bit


def _case(cid, calls, expected, actual, locations):
    return {"id": cid, "calls": calls, "expected": expected,
            "actual": actual, "locations": locations}


def _case_rr_shared_coin_overlap():
    """RR-01: overlapping same-epoch cohorts share coins; exact ratio 3."""
    calls = [
        ["revise", [["ada", 1, 0], ["bo", 1, 1]]],
        ["submit", "j1", "e", ["ada", "bo"]],
        ["prepare", "j1", "w", 0],
        ["push", "h1"],
        ["submit", "j2", "e", ["ada"]],
        ["prepare", "j2", "w", 1],
        ["push", "h2"],
    ]
    spec = {"cap": 5, "nodes": [
        {"op": "revise", "at": "i", "next": 1},
        {"op": "submit", "job": "j1", "epoch": "e", "members": ["ada", "bo"], "next": 2},
        {"op": "prepare", "job": "j1", "worker": "w", "now": 0, "save": "h1", "next": 3},
        {"op": "push", "handle": "h1", "next": 4},
        {"op": "submit", "job": "j2", "epoch": "e", "members": ["ada"], "next": 5},
        {"op": "prepare", "job": "j2", "worker": "w", "now": 1, "save": "h2", "next": 6},
        {"op": "push", "handle": "h2", "next": -1},
    ]}
    left = {"i": [["ada", 1, 0], ["bo", 1, 1]]}
    right = {"i": [["ada", 1, 1], ["bo", 1, 1]]}
    cmp_ = compare(spec, left, right)
    # Independent expectation: ada has ONE coin reused in both jobs, so the
    # single-person likelihood ratio is at most 3; exhaustive 16 tapes below.
    support = {}
    for tape in __import__("itertools").product(range(4), repeat=2):
        r = Receiver()
        p = Publisher(r, cap=5)
        q = list(tape)
        p.revise(left["i"])
        p.submit("j1", "e", ["ada", "bo"])
        p.push(p.prepare("j1", "w", 0, draw=lambda: q.pop(0)))
        p.submit("j2", "e", ["ada"])
        p.push(p.prepare("j2", "w", 1, draw=lambda: q.pop(0)))
        key = tuple(o["payload"]["count"] for o in r.observed())
        support[key] = support.get(key, 0) + 1
    actual = {"compare_ratio": cmp_["ratio"],
              "left_probability": cmp_["left_probability"],
              "right_probability": cmp_["right_probability"],
              "tape_weights_ada0": {str(k): v for k, v in sorted(support.items())}}
    expected = {"max_ratio_fraction": [3, 1],
                "tape_space": 16,
                "expected_tape_weights_ada0": {"(0, 0)": 3, "(1, 0)": 9,
                                               "(1, 1)": 1, "(2, 1)": 3}}
    return _case("RR-01-shared-coin-overlap", calls, expected, actual,
                 [L["coin_memo"], L["count_fn"], L["prepare_payload"]])


def _case_revision_support_mismatch():
    """RR-02: corrections reuse the epoch coin and create disjoint support."""
    calls = [
        ["revise", [["ada", 1, 0]]],
        ["submit", "j1", "e", ["ada"]], ["prepare", "j1", "w", 0], ["push", "h1"],
        ["revise", [["ada", 2, 0]]],
        ["submit", "j2", "e", ["ada"]], ["prepare", "j2", "w", 1], ["push", "h2"],
        ["revise", [["ada", 3, 1]]],
        ["submit", "j3", "e", ["ada"]], ["prepare", "j3", "w", 2], ["push", "h3"],
    ]
    spec = {"cap": 5, "nodes": [
        {"op": "revise", "at": "r0", "next": 1},
        {"op": "submit", "job": "j1", "epoch": "e", "members": ["ada"], "next": 2},
        {"op": "prepare", "job": "j1", "worker": "w", "now": 0, "save": "h1", "next": 3},
        {"op": "push", "handle": "h1", "next": 4},
        {"op": "revise", "at": "r1", "next": 5},
        {"op": "submit", "job": "j2", "epoch": "e", "members": ["ada"], "next": 6},
        {"op": "prepare", "job": "j2", "worker": "w", "now": 1, "save": "h2", "next": 7},
        {"op": "push", "handle": "h2", "next": 8},
        {"op": "revise", "at": "r2", "next": 9},
        {"op": "submit", "job": "j3", "epoch": "e", "members": ["ada"], "next": 10},
        {"op": "prepare", "job": "j3", "worker": "w", "now": 2, "save": "h3", "next": 11},
        {"op": "push", "handle": "h3", "next": -1},
    ]}
    left = {"r0": [["ada", 1, 0]], "r1": [["ada", 2, 0]], "r2": [["ada", 3, 1]]}
    right = {"r0": [["ada", 1, 0]], "r1": [["ada", 2, 1]], "r2": [["ada", 3, 1]]}
    cmp_ = compare(spec, left, right)
    # Production confirmation with the single memoized coin over all four draws.
    observed = {}
    for history, tag in ((left, "D"), (right, "Dprime")):
        supports = []
        for draw in range(4):
            r = Receiver()
            p = Publisher(r, cap=5)
            p.revise(history["r0"])
            p.submit("j1", "e", ["ada"])
            p.push(p.prepare("j1", "w", 0, draw=lambda: draw))
            p.revise(history["r1"])
            p.submit("j2", "e", ["ada"])
            p.push(p.prepare("j2", "w", 1, draw=lambda: draw))
            p.revise(history["r2"])
            p.submit("j3", "e", ["ada"])
            p.push(p.prepare("j3", "w", 2, draw=lambda: draw))
            supports.append(tuple(o["payload"]["count"] for o in r.observed()))
        observed[tag] = sorted(list(x) for x in set(supports))
    expected = {
        "ratio": "infinity",
        "support_D": [[0, 0, 1], [1, 1, 0]],
        "support_Dprime": [[0, 1, 1], [1, 0, 0]],
        "witness_must_lie_in_support_D": True,
        "reason": "bit vector (0,0,1) is neither equal nor the pointwise "
                  "complement of (0,1,1); one shared 4-valued coin cannot "
                  "realize both, so positive/zero probability events exist",
    }
    actual = {"compare_ratio": cmp_["ratio"],
              "witness_counts": [o["payload"]["count"]
                                 for t in cmp_["witness"] for o in t["observations"]],
              "production_support": observed}
    return _case("RR-02-revision-disjoint-support", calls, expected, actual,
                 [L["coin_memo"], L["revise"], L["prepare_payload"]])


def _case_lost_ack_refund():
    """RR-03: receiver commit + lost response + cancel refunds a charged unit."""
    calls = [
        ["revise", [["ada", 1, 0]]],
        ["submit", "lost", "may", ["ada"]],
        ["prepare", "lost", "w1", 0],
        ["push", "h", "after_receiver"],
        ["cancel", "lost"],
        ["submit", "again", "jun", ["ada"]],
        ["prepare", "again", "w2", 1],
        ["push", "h2"],
    ]
    r = Receiver()
    p = Publisher(r, cap=1)
    p.revise([["ada", 1, 0]])
    p.submit("lost", "may", ["ada"])
    h = p.prepare("lost", "w1", 0, draw=lambda: 0)
    stop_name = None
    try:
        p.push(h, fail="after_receiver")
    except Stop as exc:
        stop_name = str(exc)
    usage_before = p.usage("ada")
    cancel_result = p.cancel("lost")
    usage_after = p.usage("ada")
    p.submit("again", "jun", ["ada"])
    h2 = p.prepare("again", "w2", 1, draw=lambda: 0)
    p.push(h2)
    actual = {
        "stop": stop_name,
        "receiver_observations": r.observed(),
        "usage_before_cancel": usage_before,
        "cancel_result": cancel_result,
        "usage_after_cancel": usage_after,
        "final_usage": p.usage("ada"),
        "distinct_persisted_coins": p.db.execute(
            "SELECT count(*) FROM coins").fetchone()[0],
    }
    expected = {
        "stop": "AFTER_RECEIVER",
        "observations_emitted": 2,
        "independent_epoch_coins": 2,
        "usage_before_cancel": 1,
        "usage_after_cancel": 0,
        "final_usage_under_cap_1": 1,
        "privacy_loss_units": "2*ln3 > cap*ln3 = ln3",
    }
    return _case("RR-03-lost-ack-cancel-refund", calls, expected, actual,
                 [L["push"], L["receiver_emit"], L["cancel"], L["usage"], L["admission"]])


def _case_cancel_before_exposure_reclaim():
    """RR-04: cancellation with no possible exposure is safely reclaimable."""
    calls = [
        ["revise", [["ada", 1, 0]]],
        ["submit", "j", "e", ["ada"]],
        ["prepare", "j", "w", 0],
        ["cancel", "j"],
        ["submit", "k", "f", ["ada"]],
        ["prepare", "k", "w", 1],
        ["push", "h2"],
    ]
    r = Receiver()
    p = Publisher(r, cap=1)
    p.revise([["ada", 1, 0]])
    p.submit("j", "e", ["ada"])
    p.prepare("j", "w", 0, draw=lambda: 0)
    cancel_result = p.cancel("j")
    p.submit("k", "f", ["ada"])
    h2 = p.prepare("k", "w", 1, draw=lambda: 1)
    p.push(h2)
    actual = {"cancel_result": cancel_result,
              "receiver_observations": r.observed(),
              "usage": p.usage("ada"),
              "coins": [list(row) for row in p.db.execute(
                  "SELECT epoch,person,draw FROM coins ORDER BY 1,2")]}
    # Expected: no receiver row exists at cancel time, so no information left
    # the trust boundary; the unit is reclaimable and exactly one response
    # ships.  Note the prepared coin already persists (noise.coin memoizes at
    # prepare time), so an upgrade must retain it even though the unit returns.
    expected = {"cancel_result": "cancelled", "observations_emitted": 1,
                "usage": 1, "persisted_coin_epochs": ["e", "f"]}
    return _case("RR-04-cancel-before-exposure", calls, expected, actual,
                 [L["cancel"], L["receiver_emit"], L["admission"], L["coin_memo"]])


def _case_stale_push_after_cancel():
    """RR-05: a delayed handle still emits after cancellation; push checks state late."""
    calls = [
        ["revise", [["ada", 1, 0]]],
        ["submit", "j", "e", ["ada"]],
        ["prepare", "j", "w", 0],
        ["cancel", "j"],
        ["push", "stale_handle"],
    ]
    r = Receiver()
    p = Publisher(r, cap=1)
    p.revise([["ada", 1, 0]])
    p.submit("j", "e", ["ada"])
    handle = p.prepare("j", "w", 0, draw=lambda: 0)
    cancel_result = p.cancel("j")
    error = None
    try:
        p.push(handle)
    except ValueError as exc:
        error = str(exc)
    actual = {"cancel_result": cancel_result, "push_error": error,
              "receiver_observations": r.observed(),
              "job_state": p.db.execute("SELECT state FROM jobs WHERE id='j'").fetchone()[0]}
    # Expected under the contract's uncertainty rule: the local call fails, but
    # the receiver has already appended, so this cancellation is NOT provably
    # unexposed and the budget must stay charged.
    expected = {"push_error": "STALE", "observations_emitted": 1,
                "job_state": "cancelled",
                "required_treatment": "keep charged; uncertainty != proof of no exposure"}
    return _case("RR-05-stale-push-after-cancel", calls, expected, actual,
                 [L["push"], L["receiver_emit"], L["cancel"]])


def _case_takeover_controls():
    """RR-06: BUSY before expiry, equality-time takeover, payload stability."""
    calls = [
        ["revise", [["ada", 1, 1]]],
        ["submit", "j", "e", ["ada"]],
        ["prepare", "j", "w1", 0, 3],
        ["prepare", "j", "w2", 2],
        ["prepare", "j", "w2", 3],
        ["push", "takeover_handle"],
        ["prepare", "j", "w3", 9],
    ]
    r = Receiver()
    p = Publisher(r, cap=2)
    p.revise([["ada", 1, 1]])
    p.submit("j", "e", ["ada"])
    first = p.prepare("j", "w1", 0, ttl=3, draw=lambda: 0)
    busy = None
    try:
        p.prepare("j", "w2", 2)
    except ValueError as exc:
        busy = str(exc)
    takeover = p.prepare("j", "w2", 3)
    p.push(takeover)
    after_done = p.prepare("j", "w3", 9)
    actual = {"busy_error": busy,
              "payload_identical": first["payload"] == takeover["payload"],
              "prepare_after_done": after_done,
              "receiver_observations": r.observed(),
              "coin_rows": p.db.execute("SELECT count(*) FROM coins").fetchone()[0]}
    expected = {"busy_error": "BUSY", "payload_identical": True,
                "prepare_after_done": None, "observations_emitted": 1,
                "coins_created": 1}
    return _case("RR-06-lease-takeover", calls, expected, actual,
                 [L["prepare_payload"], L["push"]])


def _case_payload_stable_across_revision():
    """RR-07: cached payload survives corrections; only fresh jobs see new bits."""
    calls = [
        ["revise", [["ada", 1, 0]]],
        ["submit", "j", "e", ["ada"]],
        ["prepare", "j", "w", 0],
        ["revise", [["ada", 2, 1]]],
        ["prepare", "j", "w", 5],
        ["push", "h"],
        ["submit", "k", "e", ["ada"]],
        ["prepare", "k", "w", 6],
        ["push", "h2"],
    ]
    r = Receiver()
    p = Publisher(r, cap=2)
    p.revise([["ada", 1, 0]])
    p.submit("j", "e", ["ada"])
    first = p.prepare("j", "w", 0, draw=lambda: 0)
    p.revise([["ada", 2, 1]])
    renewed = p.prepare("j", "w", 5, draw=lambda: 3)  # draw must be ignored
    p.push(renewed)
    p.submit("k", "e", ["ada"])
    fresh = p.prepare("k", "w", 6, draw=lambda: 0)
    p.push(fresh)
    actual = {"first_payload": first["payload"], "renewed_payload": renewed["payload"],
              "fresh_payload": fresh["payload"],
              "observations": r.observed(),
              "coins": [list(row) for row in p.db.execute(
                  "SELECT epoch,person,draw FROM coins ORDER BY 1,2")]}
    # draw=0 truthfully reports the bit; j keeps the old bit 0, k sees bit 1.
    expected = {"j_count": 0, "k_count": 1, "single_shared_coin_draw": 0,
                "stable_payload": True}
    return _case("RR-07-payload-stability-revision", calls, expected, actual,
                 [L["prepare_payload"], L["coin_memo"], L["revise"]])


_WORKER_SRC = """
import os, sys
sys.path.insert(0, {root!r})
from release import Publisher, Receiver
r = Receiver({receiver!r})
p = Publisher(r, path={db!r}, cap=1)
p.revise([["ada", 1, 0]])
p.submit("j", "may", ["ada"])
h = p.prepare("j", "w1", 0, draw=lambda: 0)
try:
    p.push(h, fail="after_receiver")
except RuntimeError:
    pass
print("WORKER_COMMITTED_EXTERNALLY")
"""

_RECOVERY_SRC = """
import sys
sys.path.insert(0, {root!r})
from release import Receiver
from release import Publisher
r = Receiver({receiver!r})
p = Publisher(r, path={db!r}, cap=1)
obs = r.observed()
state = p.db.execute("SELECT state,owner,payload FROM jobs WHERE id='j'").fetchone()
usage = p.usage("ada")
import json
print(json.dumps({{"observations": obs, "job_row": list(state), "usage": usage}}))
"""


def _case_worker_death_and_reopen():
    """RR-08: real worker process termination, then reopen both durable databases."""
    calls = [
        ["subprocess: revise/submit/prepare/push(after_receiver); process exits"],
        ["reopen", "publisher.sqlite", "receiver.sqlite"],
        ["inspect", "receiver.observed", "jobs row", "usage"],
    ]
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "publisher.sqlite")
        recv = os.path.join(tmp, "receiver.sqlite")
        worker = os.path.join(tmp, "worker.py")
        recovery = os.path.join(tmp, "recover.py")
        Path(worker).write_text(_WORKER_SRC.format(root=str(ROOT), db=db, receiver=recv))
        Path(recovery).write_text(_RECOVERY_SRC.format(root=str(ROOT), db=db, receiver=recv))
        proc = subprocess.run([sys.executable, "-I", worker], capture_output=True,
                              text=True, cwd=tmp)
        worker_out = proc.stdout.strip()
        rec_proc = subprocess.run([sys.executable, "-I", recovery],
                                  capture_output=True, text=True, cwd=tmp)
        reopened = json.loads(rec_proc.stdout.strip())
    actual = {"worker_stdout": worker_out,
              "worker_returncode": proc.returncode,
              "reopened": reopened}
    expected = {
        "worker_stdout": "WORKER_COMMITTED_EXTERNALLY",
        "receiver_observation_survives": 1,
        "local_job_state": "ready",
        "usage_charged": 1,
        "note": "durable receiver commit cannot be retracted; recovery must "
                "replay the same payload, never refund on uncertainty",
    }
    return _case("RR-08-worker-death-reopen", calls, expected, actual,
                 [L["push"], L["receiver_emit"], L["tx"], L["prepare_payload"]])


def _case_adaptive_compare():
    """RR-09: adaptive branch strategy, exact 9 = 3^2 across two epoch coins."""
    calls = [
        ["revise"], ["submit", "j1", "e"], ["prepare"], ["push"],
        ["branch", "j1 == 1"],
        ["submit/prepare/push j2 (yes) or j3 (no)"],
    ]
    spec = {"cap": 5, "nodes": [
        {"op": "revise", "at": "i", "next": 1},
        {"op": "submit", "job": "j1", "epoch": "e", "members": ["ada"], "next": 2},
        {"op": "prepare", "job": "j1", "worker": "w", "now": 0, "save": "h1", "next": 3},
        {"op": "push", "handle": "h1", "next": 4},
        {"op": "branch", "job": "j1", "equals": 1, "yes": 5, "no": 8},
        {"op": "submit", "job": "j2", "epoch": "f", "members": ["ada"], "next": 6},
        {"op": "prepare", "job": "j2", "worker": "w", "now": 1, "save": "h2", "next": 7},
        {"op": "push", "handle": "h2", "next": -1},
        {"op": "submit", "job": "j3", "epoch": "g", "members": ["ada"], "next": 9},
        {"op": "prepare", "job": "j3", "worker": "w", "now": 2, "save": "h3", "next": 10},
        {"op": "push", "handle": "h3", "next": -1},
    ]}
    left = {"i": [["ada", 1, 0]]}
    right = {"i": [["ada", 1, 1]]}
    cmp_ = compare(spec, left, right)
    swapped = compare(spec, right, left)
    actual = {"forward_ratio": cmp_["ratio"], "reverse_ratio": swapped["ratio"],
              "forward_probs": [cmp_["left_probability"], cmp_["right_probability"]],
              "witness": cmp_["witness"]}
    expected = {"forward_ratio": [9, 1], "reverse_ratio": [9, 1],
                "reason": "two independent (epoch,person) coins, each ln3, "
                          "and the branch only selects which second epoch runs"}
    return _case("RR-09-adaptive-branch-comparison", calls, expected, actual,
                 [L["coin_memo"], L["count_fn"]])


def _case_sample_policy_compare():
    """RR-10: shipped sample-policy/paired-histories exhibit infinite loss."""
    calls = [["policy.run", "sample-policy.json", "paired-histories.json"],
             ["review_evidence.compare(spec, left, right)"]]
    spec = json.loads((ROOT / "sample-policy.json").read_text())
    histories = json.loads((ROOT / "paired-histories.json").read_text())
    cmp_ = compare(spec, histories["left"], histories["right"])
    real_trace = policy_run(spec, histories["left"])
    actual = {"ratio": cmp_["ratio"],
              "left_probability": cmp_["left_probability"],
              "right_probability": cmp_["right_probability"],
              "witness_matches_policy_shape": cmp_["witness"][0].keys() == real_trace[0].keys(),
              "witness_nodes": [t["node"] for t in cmp_["witness"]]}
    expected = {"ratio": "infinity",
                "left_probability_nonzero": True,
                "right_probability": [0, 1],
                "witness_shape": "policy.run trace entries"}
    return _case("RR-10-sample-policy-infinity", calls, expected, actual,
                 [L["coin_memo"], L["prepare_payload"], L["cancel"]])


def replay():
    """Deterministic evidence list; every actual value comes from real entries."""
    return [
        _case_rr_shared_coin_overlap(),
        _case_revision_support_mismatch(),
        _case_lost_ack_refund(),
        _case_cancel_before_exposure_reclaim(),
        _case_stale_push_after_cancel(),
        _case_takeover_controls(),
        _case_payload_stable_across_revision(),
        _case_worker_death_and_reopen(),
        _case_adaptive_compare(),
        _case_sample_policy_compare(),
    ]


def _demo():
    """Print normal and unsafe receiver transcripts plus the exact comparison."""
    import contextlib
    import io
    import checks
    with contextlib.redirect_stdout(io.StringIO()):
        normal_observations = checks.normal()
    incident = checks.exposed()
    spec = json.loads((ROOT / "sample-policy.json").read_text())
    histories = json.loads((ROOT / "paired-histories.json").read_text())
    cmp_ = compare(spec, histories["left"], histories["right"])
    print(json.dumps({
        "normal_receiver_transcript": normal_observations,
        "unsafe_receiver_transcript": incident,
        "exact_privacy_comparison": {
            "ratio": cmp_["ratio"],
            "left_probability": cmp_["left_probability"],
            "right_probability": cmp_["right_probability"],
        },
    }, indent=2, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    _demo()
