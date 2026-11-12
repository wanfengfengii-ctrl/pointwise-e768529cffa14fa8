# Count release design, review-corrected edition (supersedes RC4)

> Status: release candidate 4 **fails** the privacy contract. This document is
> the reconciled design: it states what RC4 actually guarantees, where the
> guarantee breaks, and the corrected durable design that restores pure
> user-level differential privacy at `epsilon <= cap * ln(3)` without a clean
> start. The accompanying `REVIEW.md` carries the blocking findings and
> evidence ids.

## 1. Mechanism and what RC4 actually guarantees

For one person `p` and one epoch `e`, production creates a single uniform coin
`c in {0,1,2,3}` on first use (`noise.py:15-25`, persisted in `coins`,
`release.py:63-64`) and reports the current bit truthfully for three values and
its complement for one (`noise.py:28-34`). Conditional on the bit `b`,

    Pr[reported value = b] = 3/4,  Pr[reported value = 1-b] = 1/4,

so one coin is one `ln 3` randomized-response unit. Every count in the same
`(epoch, person)` scope is a deterministic function of that same coin:
overlapping cohorts, repeated prepares and retries add no fresh randomness.
Evidence **RR-01** verifies by full 16-tape enumeration that two overlapping
same-epoch jobs have an exact worst-case ratio of `3 = 3^1`, never `9`.

For a fixed neighbour pair (histories differing only in one person's bit
history) the likelihood ratio of the whole transcript factorizes over that
person's `(epoch, person)` coins, because coins of different persons are
independent uniform draws. All other persons' coins cancel from the ratio.

## 2. The two structural defects in RC4

### 2.1 Shared coins across corrections create zero-probability support

The design claim "updating a source revision also reuses that person's draw, so
corrections do not require another unit" (RC4 doc, lines 9-11) is only safe
while the bit the coin reports never changes. When a revision lands between two
materializations in the same epoch, one coin must render *different* bits at
different times.

For a person whose materialized bit vector across same-epoch jobs is
`b = (b_1,...,b_m)` under history `D`, a single coin realizes either exactly `b`
(three draws) or exactly its pointwise complement `1-b` (one draw). Under a
neighbouring history `D'` the realizable vectors are `b'` and `1-b'`. The two
supports intersect iff `b' = b` or `b' = 1-b`. For `m >= 3` a neighbour can
change one interior revision so that `b'` is neither equal nor complementary
(RR-02 uses `b=(0,0,1)` vs `b'=(0,1,1)`): the supports are disjoint, so a
transcript positive under one history has probability zero under the other -
infinite privacy loss, which the contract forbids outright. The shipped
`sample-policy.json` / `paired-histories.json` exhibit already reaches this
state (RR-10: ratio `"infinity"`, left probability `9/64`, right probability
`0/1`). This is a failure of *support*, not of a sampled run.

### 2.2 Cancellation refunds units that may already have been spent

`push` emits to the receiver *before* any local state check
(`release.py:144-149`), and `cancel` deletes the job's reservations
unconditionally (`release.py:166-167`). After `fail="after_receiver"`, or after
a crashed/delayed worker that already emitted, the receiver holds an
append-only observation that no local ledger can erase, yet the person's
`count(DISTINCT epoch)` usage returns to zero (`release.py:107-110`). A fresh
epoch then passes admission and mints an independent coin. RR-03 shows two
independent responses for one person under `cap=1`; repeating the loop has no
fixed bound, so no finite epsilon exists for the unrestricted schedule. The
preserved incident `checks.py reproduce` is the single-job instance of this
defect. A stale handle pushed after cancellation likewise appends to the
receiver and only then fails `STALE` (RR-05): "the local call failed" must not
be read as "nothing was observed".

## 3. Necessary-and-sufficient criterion for the claimed bound

For a fixed legal call history and schedule, let `M(p,e)` be the ordered list
of jobs whose payload was ever materialized for person `p` in epoch `e`, each
labelled with the source bit visible at its *first* materialization, and let
`E(p)` be the set of epochs in which a payload for `p` was externally observed
at least once (counting an uncertain send as observed).

**Criterion.** The transcript satisfies
`Pr_D(T) <= 3^cap * Pr_D'(T)` for every neighbour pair and every measurable
event, including zero-probability support, iff both hold:

1. **Support condition.** For every person `p`, every epoch `e`, the bit vector
   materialized under `D` over `M(p,e)` and the one under `D'` are either
   pointwise equal or pointwise complementary. Equivalently: within one
   `(epoch, person)` coin scope, all materialized jobs see the same bit.
2. **Budget condition.** For every person `p`, `|E(p)| <= cap`, where every
   receiver commit - acknowledged, lost-ack, stale or replayed - stays counted
   until there is a *proof* that no send occurred.

**Argument (checkable, not a composition citation).** Coins are independent
across `(epoch, person)` keys, so the transcript likelihood is a product over
keys of per-coin factors; all keys not belonging to the changed person cancel
in the neighbour ratio. Under condition 1 the two histories induce, per key,
the same pair of deterministic output vectors (`b` and `1-b`) with weights
`3/4` and `1/4`, possibly swapped; each per-key ratio is therefore in
`[1/3, 3]` and both sides share identical support, excluding infinite loss.
The product over the observed keys has ratio in `[3^-k, 3^k]`; condition 2
bounds `k <= cap`, giving `3^cap`. Necessity: violating condition 1 yields
disjoint vector supports and hence a positive/zero transcript event (RR-02,
RR-10 construct it); violating condition 2 yields `k > cap` independent coins
and a transcript whose ratio reaches `3^k > 3^cap` (RR-03 reaches `3^2` under
`cap=1`, RR-09 shows the exact `3^k` scaling under adaptive branching). Because
every public error, phase and admission result is a deterministic function of
public metadata and the observed payloads (none reads a private bit directly,
verified against `release.py:77-168`), adaptive calls only choose which cylinder
event is reached; conditioning on the public prefix preserves the same per-key
bounds, so the criterion covers adaptive attackers without modification.

**Where no finite bound is possible.** RC4 as written enforces neither
condition: condition 1 is broken whenever corrections straddle same-epoch
materializations, and condition 2 is breakable without limit via the
emit-then-refund loop. Therefore the unrestricted RC4 system has no finite
epsilon at all; `cap*ln3` is only the bound of the subsystem restricted to
fixed per-scope bits and provably unexposed cancellations.

## 4. Corrected design (smallest compatible state change)

Keep the four-table SQLite layout, the receiver API, the lease semantics, the
job/epoch metadata model and the randomized-response primitive. Change only
how entropy is keyed, how units are reserved, and how cancellation distinguishes
proof from uncertainty.

### 4.1 Durable state (additions only)

- `coins(epoch, person, generation, draw, PRIMARY KEY(epoch,person,generation))`
  replacing the old `(epoch,person)` key. A **generation** is a
  monotonic per-`(epoch,person)` counter incremented exactly when a fresh
  payload must be materialized after the person's bit changed since the
  previous materialization in that epoch. Same-bit revisions never increment
  it, preserving free overlap and stable replay.
- `ledger(person, epoch, generation, job, state, PRIMARY KEY(job,person))`
  with `state in {'held','spent','released'}`. `held` = reserved at prepare,
  no send proven; `spent` = a receiver commit is known or cannot be ruled out;
  `released` = cancellation with a durable proof that no send happened.
- Keep `jobs(payload, owner, until, state)` exactly as today for stable
  payload replay; add nullable `send_epoch` (a local monotonic send counter,
  persisted *after* the receiver call returns) used only by recovery.
- The receiver database is unchanged and remains the source of truth for
  external exposure; it still has no delete or fencing API.

### 4.2 Entropy identity

The entropy unit is `(epoch, person, generation)`, not `(epoch, person)`. Its
draw is still one uniform `range(4)` integer created once via the production
hook and reused by every job sharing that identity. Coin creation, ledger
inserts and payload materialization happen inside the existing single
`BEGIN IMMEDIATE` prepare transaction (`release.py:67-75,115-142`), so the
entropy identity, the reservation and the payload commit atomically.

### 4.3 Admission and prepare (pseudocode)

```
prepare(job, worker, now, ttl):
  BEGIN IMMEDIATE
  row = jobs[job]; abort CANCELLED / return None if done / BUSY as today
  for person in cohort:
      g = current_generation(epoch, person)          # 0 if never seen
      bit_changed = source[person].rev > last_mat_rev(epoch, person, g)
      if bit_changed: g += 1                          # new entropy identity
      unit = (person, epoch, g)
      if ledger has no row for (job, person):
          charged = distinct (epoch,generation) with state in (held, spent)
          if not exists ledger row for unit and charged >= cap:
              rollback; raise BUDGET                  # denial is allowed
          upsert ledger(person, epoch, g, job, 'held')
      persist generation g on the job row
  if jobs[job].payload is None:
      materialize count using coins(epoch, person, g), creating draws once
  update lease exactly as today; COMMIT; return handle
```

Distinct generations, not distinct epoch names, are charged: overlapping and
repeated queries stay free, corrections pay only when they actually change a
reported bit, and the lifetime per-person cap is a cap on distinct entropy
identities ever spent.

### 4.4 Send, acknowledgement and recovery

```
push(handle, fail=None):
  BEGIN IMMEDIATE                              # local pre-check transaction
  re-validate job state == ready, owner == worker, payload matches
  record intent(job, send_counter + 1); COMMIT
  receiver.emit(job, payload)                  # external, append-only commit
  if fail == after_receiver: raise Stop        # observation already durable
  BEGIN IMMEDIATE
  if still owner and payload unchanged:
      jobs[job].state = done
      ledger where job -> state 'spent'
  COMMIT

on restart / lease takeover:
  for every job not done with an intent record:
      ask receiver for the job's bound payload (read-only observation lookup)
      if present and equal to local payload:
          mark done, ledger -> 'spent'         # replay, no new coin
      if present and different: RECEIVER_CONFLICT alarm (cannot occur via API)
      if absent: keep 'held'; retry may use the same handle and payload only
  reservations in 'held' with no intent and an expired/fenced lease may be
  released only by the rule in 4.5
```

### 4.5 Cancellation: proof of no exposure vs uncertainty

```
cancel(job):
  BEGIN IMMEDIATE
  if state == done: return done
  if intent record exists OR receiver lookup cannot prove absence:
      jobs[job].state = cancelled
      ledger rows stay 'held'/'spent'         # unit remains charged forever
      return cancelled
  else:  # no intent was ever durably recorded for this job
      jobs[job].state = cancelled
      ledger where job -> 'released'; free the generation for reuse
      return cancelled
```

Because every `push` durably records intent before the external call, "no
intent row" is a machine-checkable proof that no worker could have sent; that is
the only reclaimable case (generalizes RR-04). Anything else is treated as
possibly exposed, matching the contract's uncertainty rule and making RR-03/RR-05
unreachable as budget violations while still allowing stale workers to attempt
pushes.

### 4.6 Lease replacement and process death

Lease takeover (`now >= until`) is unchanged: it reuses the stored payload, so
it never samples and never charges again. A crashed worker's handle remains
valid for replay; recovery in 4.4 converges to `done + spent` on an observed
send or back to `held` otherwise. No transaction spans receiver I/O.

## 5. Upgrade plan for an existing RC4 deployment

No clean start, no receiver purge, no service halt.

1. **Persisted coins.** Old `coins(epoch,person,draw)` rows are imported as
   generation 0 and remain valid entropy; their `ln3` charge is preserved. New
   rows use the three-column key. An old coin is never deleted: deleting it
   would make a re-prepared job re-randomize data the receiver may already have
   correlated.
2. **Already charged jobs.** Existing `reservations` rows map to ledger
   `spent` when the job is `done`, otherwise `held`. Distinct-epoch usage under
   RC4 becomes a lower bound on distinct generations; admission treats every
   historical epoch per person as one permanently charged unit.
3. **Prepared payloads.** Cached `jobs.payload` values stay authoritative and
   are replayed unchanged; their embedded draw is looked up as generation 0.
   A revision after upgrade that changes a reported bit opens generation 1 and
   pays one unit; same-bit corrections cost nothing.
4. **Cancelled jobs of uncertain exposure.** On upgrade, every cancelled job
   whose receiver history contains its payload is converted to `spent`; every
   cancelled job with no receiver row *and* no surviving intent is `released`.
   Where RC4 recorded no intent (it did not), the conservative default is
   `spent`: the receiver is queried, and only a verified absence releases the
   unit. This may leave a person over cap; the system then denies fresh
   releases for that person rather than retracting observations - the contract
   permits denial, never erasure.
5. **Receiver observations.** Nothing is altered. Repeated identical
   observations of one job remain one entropy unit; the receiver's
   `messages`/`observations` tables already make that idempotent.
6. **Versioning.** The migration is one schema-version transaction; an old
   binary opening the new database must refuse rather than misread the coin key.

## 6. What the corrected design retains

Useful repeated and overlapping counts (free within one entropy identity),
latest-revision semantics for genuinely fresh jobs (new generation), stable
payload replay across takeover and crash, a lifetime per-person cap with
explicit BUDGET denial, and continued operation with no receiver retraction.
The finite-strategy auditor's exact engine (`review_evidence.compare`) evaluates
this design by the same criterion in section 3; the finite certificate bounds
the audited strategy and is not, by itself, a proof over unbounded schedules.
