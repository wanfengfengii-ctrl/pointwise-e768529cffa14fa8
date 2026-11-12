"""Per-person memoized randomized response used by count publication."""
import secrets


def cohort(ids):
    if not isinstance(ids, list) or not ids:
        raise ValueError("COHORT")
    if any(not isinstance(x, str) or not x for x in ids):
        raise ValueError("COHORT")
    if len(set(ids)) != len(ids):
        raise ValueError("COHORT")
    return sorted(ids)


def coin(db, epoch, person, draw=None):
    row = db.execute(
        "SELECT draw FROM coins WHERE epoch=? AND person=?", (epoch, person)
    ).fetchone()
    if row is None:
        value = secrets.randbelow(4) if draw is None else draw()
        if type(value) is not int or not 0 <= value < 4:
            raise ValueError("DRAW")
        db.execute("INSERT INTO coins VALUES(?,?,?)", (epoch, person, value))
        return value
    return row[0]


def count(db, epoch, ids, draw=None):
    answer = 0
    for person in ids:
        bit = db.execute("SELECT bit FROM source WHERE person=?", (person,)).fetchone()[0]
        value = coin(db, epoch, person, draw)
        answer += bit if value < 3 else 1 - bit
    return {"count": answer, "size": len(ids)}
