# Privacy count publisher — release review

**Decision: DO NOT RELEASE.** Release candidate 4 violates the pure
user-level differential-privacy contract in two independent, demonstrable ways:
it can emit transcripts with *infinite* privacy loss (zero probability on one
neighbouring history), and it can be driven past the lifetime budget without
limit through receiver commits whose local acknowledgement was lost or
cancelled. Both are defects of the production code, not of a sampled run; both
are reproduced through the unmodified entry points and independently verified
with exact rational probabilities.

Scope: code review and design maintenance only. `noise.py`, `release.py`,
`checks.py`, `policy.py`, `samples.json`, `sample-policy.json` and
`paired-histories.json` are preserved byte for byte (verified against the
initial commit). Deliverables are this report, the corrected
`docs/privacy-release.md`, `review_evidence.py` and `evidence.json`.

---

## 1. Evidence index

Every case is produced by `review_evidence.replay()`, stored in
`evidence.json`, re-run and compared byte-for-byte by `checks.py review`.
`expected` is computed independently of production accounting/sampling;
`actual` is freshly obtained from the real `Publisher`/`Receiver`/`policy.run`
entry points. Probabilities are integer numerator/denominator pairs only.

| Id | Establishes | Does not establish |
|---|---|---|
| **RR-01-shared-coin-overlap** | Exact worst-case ratio `3/1` for two overlapping same-epoch cohorts sharing one `(epoch,person)` coin; full 16-tape enumeration matches the symbolic engine and the `3/4–1/4` law | Does not bound schedules where the bit changes inside one coin scope |
| **RR-02-revision-disjoint-support** | Three same-epoch materializations straddling two corrections produce **disjoint transcript supports** for neighbours `(0,0,1)` vs `(0,1,1)`; `compare` returns `"infinity"` and production draws confirm both support sets | A single sampled run cannot show support mismatch; this is an exhaustive statement over all four coin values |
| **RR-03-lost-ack-cancel-refund** | Under `cap=1`, an `after_receiver` commit followed by `cancel` refunds the only unit and a second independent epoch coin is minted and published: two externally visible randomized responses, `2·ln3 > cap·ln3` | Does not by itself show the loop is unbounded; the argument in §4 generalizes it |
| **RR-04-cancel-before-exposure** | A job cancelled with no send attempt leaves no receiver observation; its unit is safely reclaimable and a later job publishes normally. Also records that the prepared coin already persists at prepare time | Does not cover stale handles still held by workers (see RR-05) |
| **RR-05-stale-push-after-cancel** | A handle retained through cancellation still appends to the receiver before the local `STALE` failure; the failed local call does not imply no exposure | The duplicate identical payload adds no new entropy for the job, but the unit must remain charged |
| **RR-06-lease-takeover** | `BUSY` before expiry, takeover at equality, identical cached payload across owners, `None` after done, one coin for the whole job | Takeover is safe only because payloads are immutable; does not address admission refunds |
| **RR-07-payload-stability-revision** | A prepared payload stays bit-stable across a revision and an ignored draw hook, while a fresh same-epoch job observes the revised bit using the same persisted coin — the exact precondition of the RR-02 support failure | Stability itself is required behaviour; the flaw is reusing one coin for the changed bit |
| **RR-08-worker-death-reopen** | A real worker subprocess terminates after the durable receiver commit; reopening both SQLite files shows the observation survives, local job stays `ready`, usage stays charged — recovery must reconcile, never assume failure | Does not test a process killed mid-transaction (SQLite rollback makes that a no-op prepare) |
| **RR-09-adaptive-branch-comparison** | A branch on a public count that selects between two second epochs has exact ratio `9/1 = 3²` in both directions; adaptive calls are covered by the per-coin factorization | Two independent coins are legitimately charged here; it demonstrates scaling, not a bug |
| **RR-10-sample-policy-infinity** | The shipped `sample-policy.json` / `paired-histories.json` exhibit already yields `"infinity"` (left `9/64`, right `0/1`) with a witness in exact `policy.run` trace shape | The finite graph is a witness, not a global proof; §5 gives the general criterion |

The `checks.py` gate behaviour is unchanged: `normal` exits 0 printing
`NORMAL_REGRESSION_OK`; `reproduce` repeatedly exits 1 printing
`PUBLISHED_WITHOUT_BUDGET`; `review` additionally replays this suite, compares
against `evidence.json` and checks that every id is referenced here.

---

## 2. Finding F1 — infinite privacy loss from coins shared across corrections

**Severity: release blocker. Contract clause: "Zero probability on one side
and positive probability on the other is an infinite privacy loss."**

### Source locations

- `noise.py:15-25` — `coin()` memoizes one draw per `(epoch, person)` forever.
- `noise.py:28-34` — every count, at any revision, reuses that draw against the
  *current* source bit.
- `release.py:63-64` — the `coins(epoch, person, draw)` primary key pins the
  scope; there is no generation/revision component.
- `release.py:137-138` — materialization calls `count()` on first prepare only;
  later jobs in the epoch inherit the same coin through their own first
  materialization.
- `release.py:77-87` — revisions replace the bit that the memoized coin later
  reports, with no entropy refresh.
- Contradicted design claim: `docs/privacy-release.md` RC4 lines 9-11
  ("Updating a source revision also reuses that person's draw, so corrections
  do not require another unit").

### Triggering calls (RR-02, RR-10)

```
revise [ada,1,b0]; submit j1 epoch e [ada]; prepare j1; push j1
revise [ada,2,b1]; submit j2 epoch e [ada]; prepare j2; push j2
revise [ada,3,b2]; submit j3 epoch e [ada]; prepare j3; push j3
```
with `D` materializing bit vector `(0,0,1)` and neighbour `D'` materializing
`(0,1,1)` (only ada's middle revision differs).

### Expected vs observed

- *Expected under the contract:* every transcript event with positive
  probability under one history has positive probability under the other, with
  ratio at most `3^cap`.
- *Observed:* under one coin `c ∈ {0,1,2,3}`, history `D` can only produce
  `(0,0,1)` (three draws) or `(1,1,0)` (one draw); `D'` can only produce
  `(0,1,1)` or `(1,0,0)`. The supports are disjoint. `compare` returns
  `ratio="infinity"` with witness counts `(1,1,0)`, left probability `1/4`-class
  weight, right probability `0/1`. RR-02's production loop over all four draws
  confirms exactly those support sets; RR-10 shows the shipped sample reaches
  the same state (`9/64` vs `0/1`).

### Root cause

One 4-valued coin can only express two output vectors for a person — a vector
and its pointwise complement. Memoizing it across a bit change asks it to
express a third vector, which has probability zero. This is a support failure,
so no epsilon, however large, can rescue it; composition accounting is
irrelevant.

### Minimal compatible repair

Key entropy by `(epoch, person, generation)` and increment the generation only
when a fresh materialization would report a changed bit (design doc §4.1–4.3).
Same-bit corrections and all overlapping/repeated queries keep sharing
generation 0 for free; changed bits buy a new `ln3` unit through the normal
admission gate. Persisted old coins migrate as generation 0 (§5 of the design
doc), so no re-randomization of already published data occurs.

---

## 3. Finding F2 — cancellation and lost acknowledgements refund spent units

**Severity: release blocker. Contract clauses: "No receiver observation may be
ignored because a local call failed"; "uncertainty about an external send must
be distinguished from proof of no exposure"; lifetime cap for the entire
externally visible transcript.**

### Source locations

- `release.py:144-149` — `push()` calls `receiver.emit` **first**; the
  `after_receiver` failure and any crash after that point leave an
  append-only external observation with the local job still `ready`.
- `release.py:26-38` — receiver commit is unconditional and append-only; there
  is no delete/fence API by construction.
- `release.py:159-168` — `cancel()` sets `cancelled` and deletes the job's
  reservation rows with **no exposure check** (`release.py:166-167`).
- `release.py:107-110` — `usage = count(DISTINCT epoch) FROM reservations`;
  deletion makes the spent epoch vanish from accounting.
- `release.py:129-136` — admission then treats a brand-new epoch as affordable
  and `noise.coin` mints an independent draw (`noise.py:19-24`).
- `release.py:150-157` — the post-emit ownership/payload checks explain why a
  stale worker's push ends in `STALE`, but only *after* the receiver append.

### Triggering calls (RR-03; single-job form preserved in `checks.py reproduce`)

```
revise [ada,1,0]; submit lost may [ada]; prepare lost w1; push lost fail=after_receiver
cancel lost                                   # usage 1 -> 0, receiver still holds seq 1
submit again jun [ada]; prepare again w2; push again   # independent coin, second response
```

### Expected vs observed

- *Expected:* with `cap=1` at most one externally visible randomized response
  per person, so total epsilon ≤ `ln3`; an uncertain or confirmed send keeps
  the unit charged.
- *Observed (RR-03 actual):* two receiver observations from two distinct
  persisted coins (`may`, `jun`), `usage` returns to 0 after cancel and ends at
  1 under cap 1. Each coin is an independent `ln3` unit, so the transcript
  reaches `2·ln3`. Nothing bounds the number of emit/cancel iterations, hence
  the unrestricted system has no finite epsilon. RR-05 shows the same exposure
  path through a stale handle: the receiver logs the payload first and the
  caller only then receives `STALE`.

### Root cause

The ledger treats "local job not done" as "unit not spent", while the privacy
boundary is the receiver, not the local transaction. Cancellation conflates
provably-never-sent jobs with possibly-sent jobs, and the reservation table is
the only accounting record, so deleting it erases evidence that cannot be
recreated (the receiver cannot be asked to forget, and in RC4 cannot even be
queried per job beyond its own tables).

### Minimal compatible repair

1. Durable **send intent** record written in a local transaction before every
   receiver call; reconciliation on restart/takeover against the receiver's
   job-keyed payload, marking the unit `spent` on any observed or unprovable
   send (design doc §4.4).
2. A three-state per-`(job,person)` ledger `held/spent/released`; `cancel`
   releases a unit only when no intent exists — a machine-checkable proof of
   no exposure — and otherwise keeps it charged forever (§4.5). RR-04 is the
   legitimately reclaimable case and remains supported.
3. Move the local ownership/payload validation before the external emit where
   feasible so an obviously stale handle fails closed without an append; this
   cannot replace the intent rule for handles that were valid when captured.

The receiver API and its append-only semantics stay untouched; no observation
is ever retracted.

---

## 4. Necessary-and-sufficient criterion for `epsilon ≤ cap·ln3`

For a fixed legal history/schedule define, per person `p` and epoch `e`:

- `M(p,e)`: jobs whose payload was materialized for `p` in `e`, ordered, each
  tagged with the bit visible at first materialization;
- `E(p)`: entropy identities (RC4: distinct epochs; corrected design: distinct
  `(epoch, generation)` pairs) for which a receiver observation of a payload
  involving `p` is confirmed or cannot be ruled out.

**Criterion.** The bound `Pr_D(T) ≤ 3^cap · Pr_D'(T)` holds for every
neighbour pair and every measurable transcript event iff:

1. **Support condition:** for each `(p,e)`, the materialized bit vectors under
   `D` and `D'` are pointwise equal or pointwise complementary (equivalently,
   one coin scope never reports two different bits of the same person); and
2. **Budget condition:** `|E(p)| ≤ cap` for every person, counting every
   uncertain send as exposed.

**Checkable argument.**

- *Factorization:* coins are independent across `(epoch,person)` keys
  (`secrets.randbelow` at first creation, `noise.py:19-24`); a neighbour
  changes one person, so every coin of every other person cancels in the
  likelihood ratio. The transcript probability is a product of per-key factors.
- *Per-key ratio:* under condition 1 the two histories induce the same two
  deterministic output vectors with probabilities `3/4` and `1/4` (possibly
  swapped). Each factor therefore lies in `[1/3, 3]` and both sides assign
  positivity to exactly the same events — no infinite-loss event exists.
- *Product:* with `k = |E(p)|` observed keys the ratio is bounded by
  `3^k`; condition 2 gives `k ≤ cap`, hence `3^cap`. RR-01 certifies the
  single-key bound `3/1` exactly; RR-09 certifies `3^k = 9/1` for two
  independent keys under an adaptive branch, in both directions.
- *Necessity:* a condition-1 violation constructs disjoint vector supports and
  therefore a positive/zero event (RR-02, RR-10 are explicit constructions); a
  condition-2 violation yields `k > cap` independent coins and a transcript
  attaining ratio `3^k` (RR-03 attains `3²` under cap 1; the loop has no fixed
  retry bound, so arbitrary `k` is reachable in RC4).
- *Adaptivity:* all public outcomes other than payload counts — submit state,
  `BUSY`/`BUDGET`/`CANCELLED`/`STALE`/revision errors, phases — are
  deterministic functions of public metadata (`release.py:77-168` reads no
  private bit in those branches). Branching on the public transcript therefore
  only selects which cylinder event is reached; conditioning on each public
  prefix preserves the same per-key ratio bounds, which is exactly how the
  symbolic engine handles branch nodes. No timing/internal-table channels are
  in scope.

**Delimitation.** RC4 enforces neither condition, so for the unrestricted
release policy no finite bound exists: F1 breaks support on any schedule with
corrections inside one epoch, F2 breaks the budget on any schedule allowed to
lose acks and cancel. Within schedules satisfying both conditions — fixed bits
per coin scope, provably-unexposed cancellations only — RC4 does provide the
claimed bound, as RR-01/RR-09 verify exactly.

### How `review_evidence.compare` evaluates the criterion

`compare(spec, left, right)` (`review_evidence.py`) returns the tight maximum
of `Pr_left(T)/Pr_right(T)` over complete finite transcripts as a reduced
integer pair, or `"infinity"` with a positive-left/zero-right witness, plus
both exact probabilities and a witness in the exact `policy.run` trace shape.

- It symbolically executes every root-to-termination walk of the forward DAG,
  mirroring `release.py` semantics (admission, memoization, cached payloads,
  receiver append/duplicate semantics, branch edges, error names).
- Randomness is represented per `(epoch,person)` key with values `range(4)`.
  The convolution is factorized person by person — only each person's local
  `4^k` draw vector is enumerated, lanes that contradict an already-fixed
  branch are pruned, and weights are integer counts; neither the complete joint
  random tape nor any private database is enumerated. No polynomial guarantee
  is claimed, per contract.
- Branch feasibility keeps a lane alive while required coins are undecided and
  prunes only on contradicted edges, so adaptive strategies are handled
  natively; probabilities normalize to 1 by an internal assertion.
- Witnesses are replayed through the **real, unmodified** `policy.run` via a
  production draw hook fed in the exact first-creation order (captured with an
  instrumented `noise.coin` wrapper), and asserted equal to the symbolic trace.
- Every engine distribution in the evidence is additionally cross-checked by
  exhaustive enumeration of all `4^m` coin tables through the real entry
  points (RR-01, RR-02, RR-09 and the development cross-checks), and all
  probabilities are `Fraction`/integer pairs — no floating-point thresholds.
- Swapping the two histories returns the reverse-direction certificate; both
  directions are asserted in RR-09. The certificate bounds the audited finite
  strategy only; the unrestricted-policy claim rests on the criterion and the
  repaired ledger design above.

---

## 5. Repair alternative comparison and upgrade boundaries

### Alternatives considered

1. **Generation-keyed coins + held/spent/released ledger (chosen).** Smallest
   change: one extra key column, one new ledger table, one pre-send intent row;
   all existing APIs, SQLite isolation and receiver behaviour are preserved.
   Keeps free overlap/replay, charges only genuine bit changes, makes
   cancellation proof-based.
2. **Global fresh coin per job.** Simpler reasoning but destroys the
   memoization property the product requires: overlapping cohorts would each
   cost a unit, violating the "retain useful repeated and overlapping counts"
   requirement and inflating budget use.
3. **Refuse all revisions after first prepare.** Restores support trivially but
   breaks latest-revision semantics for fresh jobs, an explicit required
   behaviour, and does nothing for F2.
4. **Synchronous receiver ack inside the local transaction / distributed
   transaction.** Would remove the lost-ack window but the receiver has no such
   API and holding a SQLite write transaction across external I/O is precisely
   what the current design avoids; it also cannot fix observations already made.
5. **Clean database and reissue.** Explicitly forbidden: it cannot retract
   receiver observations and would re-randomize published data.
6. **Halt all releases on uncertainty.** Forbidden by the contract; the chosen
   design denies only genuinely unaffordable fresh releases per person.

### Upgrade boundaries (design doc §5 has the full plan)

- **Old random coins:** migrated in place as generation 0; never deleted, since
  deletion would allow a second draw over data already correlated externally.
- **Old payloads:** remain the authoritative replay payloads; their draw is
  looked up as generation 0.
- **Already charged jobs:** `done` maps to `spent`; open jobs map to `held`;
  historical distinct epochs permanently count as charged units.
- **Possibly exposed cancelled jobs:** receiver is queried per job; observed →
  `spent`, verified absent with no intent → `released`; RC4's missing intent
  records default to the conservative `spent` classification. A person found
  over cap stays served for replays but is denied fresh units — denial, not
  erasure.
- **Lost responses and crashes:** recovery converges via intent reconciliation;
  RR-08 demonstrates the durable state that recovery starts from on the actual
  reopened databases.
- A schema-version gate prevents an old binary from interpreting the new coin
  key. No step assumes the receiver can retract anything.

---

## 6. Persistence and recovery summary (executable-level pseudocode)

Full pseudocode is in `docs/privacy-release.md` §4.1–4.6. Invariants the
implementation must maintain, each directly checkable in SQLite:

1. Coin rows are immutable once inserted; identity includes generation.
2. Coin creation, ledger upsert and payload insert commit in one
   `BEGIN IMMEDIATE` transaction per prepare.
3. Every receiver call is preceded by a durable intent row; no intent row
   exists without a subsequent reconciliation decision.
4. A ledger row leaves `held` only to `spent` (observed/unprovable) or
   `released` (proof of no send); `spent` is terminal.
5. Admission counts terminal+held distinct identities per person against
   `cap`; `BUDGET` is the only over-cap outcome.
6. Job payload rows are immutable after first materialization; takeover and
   restart reuse them byte-for-byte (RR-06, RR-07, RR-08).

---

## 7. Reproduction

```
python3 checks.py normal       # NORMAL_REGRESSION_OK, exit 0 (preserved)
python3 checks.py reproduce    # PUBLISHED_WITHOUT_BUDGET, exit 1 (preserved incident)
python3 checks.py review       # replay + evidence.json comparison + report refs
python3 review_evidence.py     # normal and unsafe transcripts + exact comparison
```

`.pointwise-runtime.json` now points `verify` at `checks.py review` and `demo`
at `review_evidence.py`, per contract. All evidence commands run with the
Python standard library only; subprocess evidence (RR-08) runs isolated
(`-I`) against paths passed as arguments, with the repository root inserted
explicitly so no untrusted working-directory module is imported.
