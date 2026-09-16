# Booking-to-billing failure cases

A small, readable sample of the part of a booking integration that is not the
domain: third party reservations arriving by push and by nightly file, turned
into billable obligations, turned into a monthly run that talks to an external
payment system.

The domain in these integrations is usually straightforward. The edges are not,
and the edges are the job. So this repository is a short memo whose evidence is
code: every case below is a test that fails without its control and passes with
it, and each one says what it costs when nobody thought about it first.

**This is not a review of anybody's architecture.** It reproduces a few
publicly described failure paths against synthetic data. Both partners are
fictional, the payment provider is a simulation with its own table, and nothing
here makes a network call or has ever held a payment key. What is assumed, what
was decided for the demonstration, and what only a partner could confirm are all
listed in [docs/what-needs-confirmation.md](docs/what-needs-confirmation.md).

Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, pytest, hypothesis.
60 tests, run on a real PostgreSQL in CI, on the schema the migration produced.

---

## Failure cases to check against your design

### 1. An external operation succeeds and its answer never comes back

The provider takes the money and the process dies before it hears anything. This
is the one case a billing run exists to survive, and it is usually tested by
hoping, because staging it properly is awkward: a mock that forgets its state
when the worker restarts cannot show the situation at all.

So the simulated provider here keeps its effects in its own table, on its own
connection, and commits them **before** the caller is told anything. At the
moment the worker dies, the operation has happened and nothing in the worker
knows it.

What the sample does about it:

* the intent is written and committed **before** the call, carrying a key
  derived from `(obligation, period)` and the frozen parameters of the request.
  A process that dies between the intent and the answer leaves a row that says
  what was attempted, which is the only thing that makes a safe resume possible;
* inside the provider's retention window, the resume presents the same key with
  the same parameters, which replays the stored result rather than repeating the
  operation;
* **outside the window, nothing is sent.** The key has been forgotten, so
  presenting it again would create a new request. The line becomes unresolved
  and waits for an outcome established another way. This is the branch that
  usually does not exist.

```
refused   billing.submit    line/1    the operation for bill:1:2026-08 was carried out and the response was lost
accepted  billing.resume    line/1    the provider replayed the operation it had already carried out

simulated provider: 3 calls, 2 operations actually carried out
```

`tests/test_lost_response.py`, seven tests, including the nominal path. That one
is not padding: without it, "no duplicate operations" is also satisfied by
nothing ever working.

**What this establishes, and what it does not.** Within this model, with this
simulated provider, a lost answer does not become a second operation. It does
not validate an integration with a real provider, which this sample does not
exercise. Reception, preparation, submission and settlement are four different
steps, and this sample stops at the third.

### 2. Two workers, at the same moment, on the same row

Two sequential calls in one process prove that a function is idempotent, which
is a much easier claim than the one that matters. What breaks in production is
two processes interleaving between somebody's read and somebody's write.

Every test in `tests/test_concurrency.py` uses two real connections to
PostgreSQL, and the race is made deterministic by asking the server whether a
connection is blocked on a lock, not by sleeping.

* the same retry delivered twice at once is applied once, settled by a unique
  index rather than by a lookup;
* two runs preparing the same period at once produce one line, because the
  second insert loses to the constraint;
* a prepared line is claimed by one atomic conditional update, so the second
  worker never gets as far as calling the external system.

### 3. Same identity, same version, different content

Two payloads carry the same booking reference and the same `updatedAt`, and
disagree. Last write wins looks like a tie break and is a coin toss with
somebody's booking; the loser is invisible, because nothing in the data
afterwards says an update was dropped.

Here neither is applied. Both are kept, the held version does not move, the
booking becomes undecidable, and the sentence to send is generated:

> These two records have the same booking ID and updatedAt but different
> statuses. Can updatedAt identify a revision, or is there a separate sequence?
> The affected booking remains on billing hold until this is resolved.

`tests/test_conflict_and_quarantine.py`. The hold is released, and the held
message replayed, by one call: a quarantine that can only be emptied by hand is
a quarantine that fills up until somebody turns the check off.

### 4. A value nobody declared, about a booking that is live and billable

Mapping an unknown status to the nearest known one is how an obligation gets
billed for a booking that was put on hold. The row is held with the question
attached, and the booking it refers to stops being prepared, because something
was said about it that nobody can read.

That the hold is conservative is a **demonstration choice**, not a
recommendation, and it is listed as one. A real product might well bill the last
confirmed state and correct later. What should not happen is for that to be
decided by whichever branch was written first.

### 5. A cancellation lands in one of three places

They are not the same place, and the instinct in all three, deleting the line,
is what makes the next run find nothing for the period and do the whole thing
again.

* before anything was submitted: voided, and that is the end of it;
* while the outcome is unknown: **nothing can be decided at all**, because
  deciding requires knowing whether money moved;
* after the operation is established: the line stays, and what is owed is a
  commercial question. No refund rule is invented here.

### 6. Where normalisation has to stop

The two sources are not two integrations. They are two incompatible
representations crossing the same boundary.

Shape can be made uniform, and the adapters do: offsets against naive local
times, a column renamed mid life with both spellings in circulation, a different
status vocabulary. Meaning cannot. Both feeds carry an integer in minor units
and a quantity, and the integer means a different thing in each: 18000 with a
quantity of 3 is 540.00 in one and 180.00 in the other. Both readings parse, no
payload says which, and the difference is on every invoice.

Each adapter declares which it was told, in writing, next to the question that
would change it. `tests/test_semantic_divergence.py`.

---

## Why the suite is worth anything

A green suite is not evidence. A test that would still pass with the thing it
claims to test removed is decoration, and it is easy to write by accident.

So every control here has a switch, and `tests/test_protections_are_load_bearing.py`
turns each one off and asserts that the dangerous outcome then happens:

| Protection removed | What then happens | Asserted |
|---|---|---|
| key derived from the line | the retry after a lost answer becomes a second operation | `total_operations() == 2` |
| intent committed before the call | the crash leaves no trace that anything was attempted | line still `prepared` |
| hold on undecidable bookings | a booking with an open question is billed anyway | line `prepared` |
| collision is not a duplicate | one of the two versions disappears silently | the other quantity is gone |

The property tests are in the same spirit. The obvious property, "any order of
deliveries reaches the same state", is **false** once collisions exist, and
writing it down was the useful part: the two orders end differently on purpose.
So there are two properties instead, convergence under a stated precondition,
and a safety invariant under collision. `tests/test_property_convergence.py`.

---

## Run it

```
docker compose up -d db
pip install -e ".[dev]"
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/kernel_dev alembic upgrade head
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/kernel_dev python scripts/demo.py
```

A committed copy of the output is in
[docs/journal-demo.txt](docs/journal-demo.txt). The interesting lines are the
refusals.

```
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/kernel_test pytest
```

Any PostgreSQL from 14 up will do, with or without the compose file: point
`TEST_DATABASE_URL` at an empty database and the suite migrates it itself. CI is
the reference run, on PostgreSQL 16, on the schema the migration produced.

---

## Layout

```
src/kernel/
  clock.py          partner timestamps as intervals, and the ordering rule
  models.py         the schema, and the guarantees the database keeps
  mutations.py      the switches that let the suite prove it notices
  journal.py        what was done, and above all what was refused
  intake/           one message in, one recorded decision out
  entitlements/     the billable obligation, deliberately two states
  billing/          preparation, the durable intent, the resume, the report
  partners/         two fictional sources that disagree on purpose
  api/              two routes, and why each status code is that one
tests/              the cases above, one file per class
docs/               what needs confirmation, one note per source, a journal
```

## Licence

MIT.
