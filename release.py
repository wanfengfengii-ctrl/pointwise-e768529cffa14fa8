"""Privacy count publisher. Source, accountant and receiver are distinct."""
import json
import sqlite3
from contextlib import contextmanager
from noise import cohort, count


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Stop(RuntimeError):
    pass


class Receiver:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS messages(
          job TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, body TEXT);
        """)

    def emit(self, job, payload):
        body = encode(payload)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT body FROM messages WHERE job=?", (job,)).fetchone()
            if old and old[0] != body:
                raise ValueError("RECEIVER_CONFLICT")
            self.db.execute("INSERT OR IGNORE INTO messages VALUES(?,?)", (job, body))
            self.db.execute("INSERT INTO observations(job,body) VALUES(?,?)", (job, body))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def observed(self):
        return [
            {"seq": seq, "job": job, "payload": json.loads(body)}
            for seq, job, body in self.db.execute(
                "SELECT seq,job,body FROM observations ORDER BY seq")
        ]


class Publisher:
    def __init__(self, receiver, path=":memory:", cap=2):
        if type(cap) is not int or cap < 1:
            raise ValueError("CAP")
        self.receiver, self.cap = receiver, cap
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS source(
          person TEXT PRIMARY KEY, rev INTEGER NOT NULL, bit INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, request TEXT NOT NULL, epoch TEXT NOT NULL,
          members TEXT NOT NULL, state TEXT NOT NULL, owner TEXT,
          until INTEGER, payload TEXT);
        CREATE TABLE IF NOT EXISTS reservations(
          job TEXT, person TEXT, epoch TEXT, PRIMARY KEY(job,person));
        CREATE TABLE IF NOT EXISTS coins(
          epoch TEXT, person TEXT, draw INTEGER, PRIMARY KEY(epoch,person));
        """)

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def revise(self, rows):
        with self.tx():
            for person, rev, bit in rows:
                if (not isinstance(person, str) or not person or
                        type(rev) is not int or rev < 1 or type(bit) is not int or bit not in (0, 1)):
                    raise ValueError("SOURCE")
                old = self.db.execute("SELECT rev,bit FROM source WHERE person=?", (person,)).fetchone()
                if old and rev == old[0] and bit != old[1]:
                    raise ValueError("REVISION_CONFLICT")
                if not old or rev > old[0]:
                    self.db.execute("INSERT OR REPLACE INTO source VALUES(?,?,?)", (person, rev, bit))

    def submit(self, job, epoch, ids):
        if any(not isinstance(x, str) or not x for x in (job, epoch)):
            raise ValueError("REQUEST")
        ids = cohort(ids)
        request = encode([epoch, ids])
        with self.tx():
            old = self.db.execute("SELECT request,state FROM jobs WHERE id=?", (job,)).fetchone()
            if old:
                if old[0] != request:
                    raise ValueError("JOB_CONFLICT")
                return old[1]
            for person in ids:
                if not self.db.execute("SELECT 1 FROM source WHERE person=?", (person,)).fetchone():
                    raise ValueError("UNKNOWN_PERSON")
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                            (job, request, epoch, encode(ids), "queued", None, None, None))
        return "queued"

    def usage(self, person):
        return self.db.execute(
            "SELECT count(DISTINCT epoch) FROM reservations WHERE person=?", (person,)
        ).fetchone()[0]

    def prepare(self, job, worker, now, ttl=10, draw=None):
        if not isinstance(worker, str) or not worker or type(now) is not int or type(ttl) is not int or ttl < 1:
            raise ValueError("LEASE")
        with self.tx():
            row = self.db.execute(
                "SELECT epoch,members,state,owner,until,payload FROM jobs WHERE id=?", (job,)
            ).fetchone()
            if row is None:
                raise ValueError("UNKNOWN_JOB")
            epoch, members, state, owner, until, payload = row
            if state == "cancelled":
                raise ValueError("CANCELLED")
            if state == "done":
                return None
            if owner is not None and owner != worker and now < until:
                raise ValueError("BUSY")
            ids = json.loads(members)
            for person in ids:
                reused = self.db.execute(
                    "SELECT 1 FROM reservations WHERE person=? AND epoch=?", (person, epoch)
                ).fetchone()
                if not reused and self.usage(person) >= self.cap:
                    raise ValueError("BUDGET")
            for person in ids:
                self.db.execute("INSERT OR IGNORE INTO reservations VALUES(?,?,?)", (job, person, epoch))
            if payload is None:
                payload = encode({"epoch": epoch, **count(self.db, epoch, ids, draw)})
            self.db.execute(
                "UPDATE jobs SET state='ready',owner=?,until=?,payload=? WHERE id=?",
                (worker, now + ttl, payload, job))
            return {"job": job, "worker": worker, "payload": json.loads(payload)}

    def push(self, handle, fail=None):
        if handle is None:
            return
        self.receiver.emit(handle["job"], handle["payload"])
        if fail == "after_receiver":
            raise Stop("AFTER_RECEIVER")
        with self.tx():
            row = self.db.execute("SELECT state,owner,payload FROM jobs WHERE id=?",
                                  (handle["job"],)).fetchone()
            if row is None or row[0] != "ready" or row[1] != handle["worker"]:
                raise ValueError("STALE")
            if row[2] != encode(handle["payload"]):
                raise ValueError("PAYLOAD")
            self.db.execute("UPDATE jobs SET state='done' WHERE id=?", (handle["job"],))

    def cancel(self, job):
        with self.tx():
            row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                raise ValueError("UNKNOWN_JOB")
            if row[0] == "done":
                return "done"
            self.db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job,))
            self.db.execute("DELETE FROM reservations WHERE job=?", (job,))
        return "cancelled"

    def snapshot(self):
        return {
            name: [list(row) for row in self.db.execute("SELECT * FROM " + name + " ORDER BY 1,2")]
            for name in ("source", "jobs", "reservations", "coins")
        }
