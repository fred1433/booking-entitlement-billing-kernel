# What needs confirmation

Most integration incidents are not caused by a bug. They are caused by a
sentence nobody wrote down: what a system guarantees on its own, and what it
can only ever inherit from a partner.

This is that sentence, split in three: what is synthetic here, what was decided
for the sake of the demonstration, and what is an open question that only a
partner can close.

## 1. What is synthetic

* **Both partners are fictional.** Coralbay Reservations and Lindhoff Booking
  Suite do not exist. They were invented to disagree with each other.
* **The payment provider is a simulation.** It is a small service with its own
  table, written to reproduce three documented behaviours of an idempotent
  create. It is not Stripe, it makes no network call, and nothing in this
  repository has ever held a payment key.
* **Nothing was sent to anybody.** Every follow up line in the reconciliation
  report is marked `Not requested, synthetic example`, and no date in it is the
  real date of a real request.
* No real architecture, specification or partner API was used. This sample
  reproduces a few publicly described failure paths. It is not a review of
  anybody's design.

## 2. What was decided for the demonstration

These are choices, not recommendations. Each one is defensible and so is its
opposite; what matters is that the choice is visible rather than an accident of
which branch was written first.

| Decision | The alternative | Why this one here |
|---|---|---|
| An undecidable booking is not prepared for billing | bill the last confirmed state and correct afterwards | it makes the effect of a held message visible in one run instead of two |
| A version collision keeps the version already held | keep the newer arrival | neither is defensible from the payload, and keeping the held one at least does not move |
| One obligation per booking | split, merge, partial obligations | anything else needs a rule nobody has given |
| Cancellation after an established operation is left for a person | automatic refund, or reversal | inventing a refund rule would be inventing a commercial policy |
| The retention window is a parameter | a constant in a comment | it is the thing that decides whether a retry is safe |

## 3. What only a partner can confirm

No amount of code on this side turns these into guarantees. Each is a sentence
somebody has to get in writing, and each is cheap to get before an integration
exists and expensive afterwards.

| Assumption | If it is wrong | What happens meanwhile |
|---|---|---|
| `(partner, external_id)` names one booking for its whole life | every rename becomes a new booking and a second obligation | nothing detects it: this is the one to confirm first |
| The event id is stable across retries | retries become new events, the booking's clock runs ahead of its content, later updates are refused as older, and nobody is told | dedup by event id, correct while the assumption holds |
| `amount` is per unit in one feed and per booking in the other | every invoice from one feed is wrong by the quantity, and every payload still parses | each adapter declares which it was told, in writing, next to the question that would change it |
| `(entitlement, period)` is the billable unit | proration, mid period changes and partial cancellations all land in the wrong place | assumed, and assumed loudly |
| The status vocabulary is complete | an unknown status is a booking in a state nobody can reason about | held with the question attached, never mapped to the nearest known value |
| Naive timestamps are in the declared zone | ordering is wrong for one hour, twice a year, on two of the busiest nights for changes | treated as a one hour window, and refused when two windows overlap |
| The nightly file is complete | a truncated file quietly cancels everything it failed to mention | structural checks plus a floor taken from this feed's own history |

## 4. What the external system's documented behaviour actually says

The submission path is shaped by four published facts about idempotent creates,
and each one removes a tempting shortcut:

1. **An idempotency key may be forgotten after 24 hours.** Presenting it after
   that does not replay anything, it creates a new request. So a retry is only
   safe inside the window, and outside it the outcome has to be established
   another way.
2. **The parameters must be identical on a retry.** A key that stays put while
   the amount moves protects nothing, which is why the parameters are frozen
   next to the intent and compared on every attempt.
3. **An indeterminate response is not a failure.** The operation may already
   have happened. The one thing that must never follow is the same intent under
   a fresh key.
4. **Webhooks are neither ordered nor deduplicated.** Anything that consumes
   them needs the same ordering and deduplication rules as the intake path, not
   a weaker set.

## 5. Why the list is the deliverable

Half the calendar time of an integration goes on getting answers to section 3.
An engineer who starts that conversation in week one, with the exact sentence to
send, gets a different answer than one who starts it in week five, when the
thing is late and the question sounds like an excuse.

The sample keeps that list itself, in `quarantine_items` and
`version_conflicts`, with the date each question was raised. The reconciliation
prints it, and says what each one is holding up. Nothing has to be remembered
by a person.
