# Count release design, release candidate 4

The public cohort is fixed by the analyst. Each source member has a revisioned
private bit. Publication returns a noisy count and the public cohort size.

For a single person, report the bit with probability 3/4 and its complement with
probability 1/4. One fresh response costs one privacy unit (epsilon = ln(3)).
Random draws are memoized by (epoch, person). Different count queries in the same
epoch may share people without additional cost: all are functions of the same
draws. Updating a source revision also reuses that person's draw, so corrections
do not require another unit.

The accountant stores one reservation per job and member. Admission counts
distinct epochs per member. BEGIN IMMEDIATE makes competing admissions serial.
The default lifetime limit is two units. Different epoch names create fresh
draws and count toward the limit; a calendar change does not reset the limit.

Preparing stores the complete payload before contacting the receiver. A new
lease reuses the stored payload, so a retry does not sample again. The receiver
binds a job ID to one payload. Thus publication is exactly once for privacy
purposes, even if the receiver logs repeated identical sends.

A job cancelled before local acknowledgement has not completed publication.
Its reservations can be returned. A worker checks ownership before marking
completion. Stale workers cannot complete, so cancellation and takeover are safe.
Only the receiver acknowledgement changes ready to done.

SQLite serializes budget and source updates; the receiver commits independently.
No process holds a source write transaction across receiver I/O. A failed send
can be recovered by taking over its expired lease and replaying the payload.
