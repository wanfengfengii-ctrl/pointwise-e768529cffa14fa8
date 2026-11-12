# Privacy count publisher review contract

## Existing entry points
Python standard library only. Run from the repository root.
Publisher(receiver, path=":memory:", cap=2) stores source data, jobs, random
coins and reservations in one SQLite database. Receiver(path=":memory:")
represents a separately committed external recipient; its observations are
append-only and are never retracted. Use paths to reopen either database.
Use separate Publisher connections for concurrent workers; no shared connection
is required. cap is fixed for a deployment. Receiver observations and submit/cancel responses
are visible to the analyst. Workers are trusted: their handles, prepared payloads,
source rows, draws and local tables are internal. An analyst can observe the job
phase or error but cannot read a prepared payload until Receiver emits it. The receiver has no deletion, fencing or distributed transaction API.

revise(rows) accepts [person, rev, bit] rows atomically. IDs are nonempty strings,
revisions are positive integers, bits are integer 0 or 1. Highest revision wins;
old revisions and identical replays have no effect. Equal revision with different
bit raises REVISION_CONFLICT and rolls back the entire call. Person identities,
the roster, revision numbers, update times and query cohorts are public; only
the bit values are private. No source person is physically removed.

submit(job, epoch, members) binds a nonempty job ID to a nonempty epoch name and
a nonempty, duplicate-free list of known person IDs; member order is immaterial.
Same request returns its current state; different request raises JOB_CONFLICT.
A cancelled job ID stays bound and cannot be resubmitted as a new request.
Epoch labels are analyst-controlled grouping keys, not replenishing periods.

prepare(job, worker, now, ttl=10, draw=None) acquires or renews a lease and returns
{job, worker, payload}, or None for done. A competing worker is BUSY before
expiry; equality permits takeover. It materializes current source values in a
single local transaction. payload is {epoch, count, size}; size is the cohort
cardinality. Already prepared payloads must remain stable across revisions,
takeover and process restart. A fresh job after a revision must use the revised
bit. draw is an internal test hook returning one integer in range(4); the public
production caller cannot choose or observe it. Production uses independent
uniform draws when a coin is first created. Each coin is shared exactly as the
current source code specifies; independence must not be assumed per query.

push(handle, fail=None) sends to Receiver and then acknowledges locally.
fail="after_receiver" raises Stop("AFTER_RECEIVER") after the external commit.
A handle may remain in another worker after cancellation, takeover or restart.
A stale worker is allowed to resume and attempt push; evidence must use these
real entry points rather than mutating tables to invent a state.
cancel(job) returns done for a completed job, otherwise returns cancelled.

The supported operational schedules include overlapping cohorts, arbitrary
source corrections between prepares, concurrent prepares, lease takeover,
cancellation, worker process death after any durable commit, delayed handles
and successful receiver commit with a lost response. There are finitely many
calls before any claimed quiescent state, but no fixed retry or job count bound.
No receiver observation may be ignored because a local call failed.

## Required release policy
The product claims pure user-level differential privacy with epsilon at most
cap * ln(3) for the entire externally visible transcript, not per job or per
epoch. Neighbouring inputs have the same public schedule and metadata but may
replace the entire bit history of one person, including multiple revisions.
For every measurable transcript event T, Pr_D(T) <= 3**cap * Pr_D'(T).
Zero probability on one side and positive probability on the other is an
infinite privacy loss. The attacker can adapt future public calls to prior
responses. Timing side channels and compromise of internal tables are outside
scope; public job states, error names and receiver payloads are inside scope.

The system must retain useful repeated and overlapping counts, latest-revision
semantics for fresh jobs, stable payload replay and a lifetime per-person cap.
It may deny a genuinely unaffordable fresh release. It must not permanently
stop all releases, assume no source corrections, make the receiver retract an
observation, or rely on a cancelled worker disappearing. Cancellation that is
provably before any possible exposure should be reclaimable; uncertainty about
an external send must be distinguished from proof of no exposure.

## Review delivery
This is a code review and design-document maintenance task, not implementation
of the corrected publication service. Preserve noise.py, release.py, checks.py
and samples.json byte for byte. Retain the normal regression and reproducible
incident. Existing docs/privacy-release.md contains release-candidate claims
and must be reconciled against the actual implementation and required policy.

Deliver REVIEW.md, corrected docs/privacy-release.md, review_evidence.py and
evidence.json. REVIEW.md must give release-blocking decisions with source file
and line ranges, root causes, triggering calls, expected and observed behaviour,
and compatible minimal repair recommendations. Distinguish a probabilistic
guarantee from a single sampled execution; independently check exact rational
transcript probabilities and support, including neighbouring bit histories.
A probability computation must account for shared coins across overlapping
queries, revisions, cached payloads and repeated receiver observations.

Give a necessary-and-sufficient criterion for the claimed bound for arbitrary
legal histories and schedules, including adaptive choices, or precisely delimit
where a finite bound is impossible. Give a checkable argument for the criterion;
an isolated counterexample or a generic composition citation is insufficient.
Provide executable-level pseudocode for the chosen correction: durable state,
entropy identity, admission, cancellation, lease replacement, send and recovery.
Compare plausible alternatives, justify the smallest compatible state changes,
and address already charged jobs, persisted coins, prepared payloads and
possibly exposed cancelled jobs on upgrade. A clean-start design alone is not
an upgrade plan. No local ledger can erase a receiver's past observation.

review_evidence.replay() takes no arguments and returns a deterministic nonempty
list of objects with id, calls, expected, actual, locations. calls records the
ordered API calls; expected is computed independently of production sampling
and accounting; actual is freshly obtained from the unmodified production
entry points; locations cites source locations. Exact probabilities use
numerator/denominator integers, never floating-point threshold comparisons.
Save that list as evidence.json. REVIEW.md references every case id and explains
what each case establishes and does not establish. Evidence includes isolated
and combined mechanisms, valid reuse and takeover controls, source revisions,
cancellation before and after possible exposure, lost acknowledgements, and
reopening the actual databases; a real worker termination must be represented.
The original source and receiver outputs must remain available in the evidence.

checks.py normal must exit 0 and print NORMAL_REGRESSION_OK.
checks.py reproduce must repeatedly exit 1 and print PUBLISHED_WITHOUT_BUDGET.
checks.py review reruns normal and replay, compares the fresh objects with
evidence.json and checks report references; its structural checks alone do not
establish the privacy theorem. Update .pointwise-runtime.json so verify is
["python3","checks.py","review"] and demo invokes review_evidence.py.
review_evidence.py when executed must show actual normal and unsafe receiver
transcripts together with the independently computed privacy comparison.

## Finite-strategy audit interface
policy.run(spec, history, draw=None) is an existing replay harness, not a privacy
oracle. sample-policy.json and paired-histories.json give a branching example.
spec has a positive cap and a nonempty nodes list. Node 0 is the entry; -1 is
termination; every other edge must point strictly forward. Fields are exactly
those demonstrated in the sample: revise reads rows from history[at], submit,
prepare, push and cancel call the corresponding API; save/handle name a trusted
worker handle. branch compares the most recent receiver count for job with the
integer equals and follows yes/no. Nodes for push may specify the existing
after_receiver failure. Other nodes use next even if a call raises an error.
The returned list records public node IDs, job phases/error names and all fresh
receiver observations in order; trusted handle contents are never included.
Both inputs to a privacy comparison have identical public metadata and differ
only in the bit history of one person. The graph and either history are finite
but have no fixed size limit. The replay harness itself and these inputs are
also preserved byte for byte; it does not replace direct recovery evidence.

As supporting evidence for the review, expose
review_evidence.compare(spec, left, right). Return
{"ratio": [numerator, denominator], "witness": transcript} for the tight maximum
of Pr_left(T)/Pr_right(T) over complete finite transcripts, or ratio="infinity"
with a positive-left/zero-right witness. When both probabilities vanish the
transcript is irrelevant. Ratios are reduced nonnegative integers with positive
denominator; witness is exactly the transcript shape returned by policy.run.
Each result also has left_probability and right_probability as reduced pairs.
Swapping histories obtains the reverse direction. The finite transcript maximum
is also the exact maximum over transcript events. The implementation must handle
arbitrary valid finite graphs with independent expectation logic, and exploit
shared state/conditional independence instead of enumerating complete random
tapes or all private databases; no polynomial runtime guarantee is demanded.
Small exhaustive calculations may independently cross-check the symbolic result.
The report must explain the certificate and its connection to the unrestricted
release policy, rather than treating this finite example as a global proof.
