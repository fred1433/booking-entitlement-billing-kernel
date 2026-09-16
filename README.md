# booking to billing integration kernel

A small, readable core for the part of a booking integration that is not the
domain: third party reservations arriving by push, pull and nightly file,
turned into entitlements, turned into a monthly billing run that must never
charge twice.

The domain here is straightforward. The edges are not, and the edges are the
whole job. So every edge in this repository is a test that fails without its
control and passes with it, and the list below says what each one costs when it
is missing.

**Both partners in this repository are fictional.** Coralbay Reservations and
Lindhoff Booking Suite do not exist. They were invented to disagree with each
other: one pushes JSON with real offsets and retries a lot, the other drops a
nightly file full of naive local timestamps, its own status vocabulary, and a
column that was renamed between two file versions. Every real integration is
somewhere between them.

Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, pytest, hypothesis.
53 tests, run on a real PostgreSQL in CI, on the schema the migration produced.

---

## What will not survive contact with real partner data

Ten things, in the order of what they cost when nobody thought about them
first. Each one links to the test that demonstrates it.

### The ones that fail silently

**1. A retry that re-stamps the envelope.**
A partner retries an event and re-serialises it on the way out, so the retried
copy carries a fresher `updated_at` while describing the same event. Treat it
as a new update and the booking now holds a timestamp from the future of its
own content. The genuine update that arrives a minute later is refused as
older. That booking has stopped receiving updates, and nothing alerts.
*The kernel dedups on the partner's event id, in a unique index, before it ever
looks at the timestamp.*
`test_a_retry_that_restamps_the_envelope_is_still_one_event`

**2. A naive local timestamp inside the hour the clocks go back.**
Two rows for the same booking at 02:30 and 02:45 local are not necessarily in
that order: either can be on either side of the change. Ordering on the string
is wrong for one hour a year, and that hour falls on one of the nights with the
most changes in it. In spring the opposite happens and an hour of readings
refers to a wall clock time that never existed.
*The kernel resolves a naive timestamp into the narrowest window that certainly
contains it, applies an update only when its window is entirely after the one
it holds, and refuses to guess when the windows overlap.*
`test_two_readings_inside_the_repeated_hour_cannot_be_ordered`,
`test_a_local_time_that_never_happened_is_held`

**3. Two versions of one booking carrying the same timestamp.**
Last write wins looks like a tie break and is a coin toss with somebody's
booking. The loser is invisible: nothing in the data afterwards says an update
was dropped.
*Equal timestamp plus equal content is a duplicate. Equal timestamp plus
different content is a conflict: the kernel keeps what it has, changes nothing,
and writes the question to send to the partner.*
`test_same_timestamp_and_different_content_is_a_conflict_not_an_overwrite`

**4. Half a nightly file.**
A truncated download applied row by row produces a database that is internally
consistent and wrong, with nothing to show which half is which. Worse, a short
file and a quiet night look identical from the outside.
*A file is applied whole or not at all. Structural checks stop it, a declared
row count is verified when the partner sends one, and a file far below this
feed's own recent median is held for a human rather than applied.*
`test_a_truncated_file_applies_nothing_at_all`,
`test_a_declared_row_count_that_does_not_match_stops_the_file`,
`test_a_file_far_shorter_than_this_feed_is_held_for_a_human`

**5. A charge whose answer never came back.**
The provider takes the money and the process dies before it hears anything. The
line is still marked prepared, so the next run charges it again. This is the
one case a billing run exists to survive, and it is the one that is usually
tested by hoping.
*The idempotency key is derived from the line, never generated per attempt, so
the retry reaches the provider as the same request and it replays its own
answer. The amount is derived from the same row, because a key that stays put
while the amount moves protects nothing.*
`test_a_charge_whose_answer_was_lost_is_not_taken_a_second_time`,
`test_the_same_key_with_a_different_amount_is_an_error_not_a_second_charge`

**6. A cancellation that lands after the charge.**
The instinct is to delete the line. Then the next run finds no line for the
period and bills it again.
*A charged line that is cancelled becomes a refund somebody has to decide
about, and it stays on the reconciliation until they do. A prepared line is
voided. Neither is ever removed.*
`test_cancelling_after_the_charge_leaves_a_refund_somebody_has_to_decide_about`

### The ones that are loud, and cheap to get right first

**7. A status value nobody told us about.**
Mapping an unknown status to the nearest known one is how an entitlement gets
activated for a booking that was put on hold.
*Held, with the sentence to send to the partner attached. Never defaulted. And
the event id of a held event is deliberately not marked as processed, so when
they resend the corrected payload we look again instead of swallowing the fix.*
`test_an_unknown_status_value_is_held_not_mapped_to_the_nearest_one`,
`test_a_quarantined_event_is_not_marked_as_processed`

**8. A column that was renamed mid life.**
Both spellings are in circulation for months, and matching a third one by
similarity is how a file gets imported into the wrong field for a week.
*Every alias is declared in writing in the adapter. An undeclared spelling
stops the file.*
`test_a_renamed_column_is_read_only_under_a_spelling_the_partner_declared`

**9. Overlapping pull windows.**
Windows have to overlap or they lose whatever was written in the gap, and
overlap is only free if reprocessing is free.
*It is, and that is checked against every order and every number of
redeliveries rather than against an opinion: whatever sequence arrives, the
booking converges on the newest state the partner sent.*
`test_overlapping_pull_windows_change_nothing_the_second_time`,
`test_any_order_and_any_number_of_redeliveries_reach_the_same_state`

**10. Two people opening the same claim link at the same moment.**
A read followed by a write lets both of them in. This one is rare until the
product is popular, which is exactly when it stops being rare.
*The check and the consumption are one statement. The test runs two real
connections against one row and proves the second one leaves empty handed.*
`test_two_people_opening_the_same_claim_link_at_once_do_not_both_get_in`

---

## Where the guarantees actually live

Five of the kernel's promises are kept by unique indexes and check constraints
rather than by service code, because service code is the part a future change
can walk around by accident.

`tests/test_database_guarantees.py` proves each one by trying to break it in
raw SQL, with the service layer out of the way, and asserts on the name of the
constraint that refused.

The full split between what the kernel guarantees and what only a partner can
confirm is in [docs/trust-boundary.md](docs/trust-boundary.md). It is the short
document that is usually missing, and the right hand column of it is where half
the calendar time of an integration goes.

---

## See it refuse things

```
docker compose up -d db
pip install -e ".[dev]"
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/kernel_dev alembic upgrade head
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/kernel_dev python scripts/demo.py
```

One night of partner traffic, printed as the kernel saw it: three bookings, two
retries, an update that arrives after the newer one, a nightly file with an
unknown status and two readings inside the repeated hour, a billing run whose
worker dies after the second charge while a third charge is taken and its
answer lost, and a cancellation that lands after the money moved.

The interesting lines are the refusals. A committed copy of the output is in
[docs/journal-demo.txt](docs/journal-demo.txt), ending in five calls to the
payment provider and four charges.

Tests:

```
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/kernel_test pytest
```

---

## Layout

```
src/kernel/
  clock.py          partner timestamps as intervals, and the ordering rule
  models.py         the schema, and the five guarantees the database keeps
  journal.py        what the kernel did, and above all what it refused
  intake/           one message in, one recorded decision out
  entitlements/     the lifecycle table, sharing, and the claim link
  billing/          the monthly run, the payment port, the reconciliation
  partners/         two fictional partners that disagree on purpose
  api/              a thin HTTP surface, and why each status code is that one
tests/              the catalogue above, one file per class of edge
docs/               the trust boundary, one note per partner, a demo journal
```

## On the payment provider

Nothing in this repository has ever held a payment key, and nothing in it makes
a network call. The kernel talks to a provider through one method. The tests
talk to a fake that keeps the two halves of the real idempotency contract: the
same key with the same request replays the first charge, and the same key with
a different request is an error rather than a second charge. Wiring a real
client is the small adapter at the bottom of `billing/payments.py` plus one
value from the environment, and a test proves the derived key reaches the call.

## Point it at your own spec

What this needs to become useful on a real integration:

* the architecture and API design, as they stand;
* the partner documentation for each feed, and a sandbox credential;
* one sentence per partner on the three assumptions in the right hand column of
  the trust boundary.

What comes back within a week: the same tests rewritten against the real
vocabulary, the real timestamp conventions and the real status lists; one
adapter per partner with its integration note; and the list of what each
partner still has to confirm, with the date each question was first asked.

## Licence

MIT.
