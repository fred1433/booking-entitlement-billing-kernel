# Coralbay Reservations, integration note

**Coralbay is a fictional partner.** It exists in this repository only, and it
was invented to behave the way push based booking partners behave.

This is the page their developer actually needs: one screen, what we receive,
what we promise, and what we are waiting on.

## How they reach us

```
POST /partners/coralbay/deliveries
Content-Type: application/json
```

One booking event per request. Any number of retries is fine.

```json
{
  "event_id": "evt-91c2",
  "booking_ref": "CB-10041",
  "state": "confirmed",
  "guest_reference": "guest-77",
  "rate_plan": "SEA-VIEW-FLEX",
  "pax": 2,
  "check_in": "2026-08-10T14:00:00+02:00",
  "check_out": "2026-08-14T10:00:00+02:00",
  "amount_minor": 24000,
  "currency": "EUR",
  "updated_at": "2026-08-04T09:15:00+02:00"
}
```

## What we answer, and what it means for their retry queue

| Answer | Meaning | What they should do |
|---|---|---|
| `200 applied` | the event changed our state | nothing |
| `200 duplicate` | we already have this `event_id` | nothing, stop retrying |
| `200 superseded` | we hold something newer for this booking | nothing |
| `202 quarantined` | we have it and cannot use it | nothing, we will come to them with a question |
| `5xx` | our fault | retry |

`202` is deliberate. Redelivering the same bytes cannot fix a value we have
never been told the meaning of, so it is our question to ask, not their message
to resend.

## What we promise

* `event_id` is deduplicated. A retried event is a no operation, every time,
  including two retries arriving on two workers at the same instant.
* An event that is older than what we hold never moves a booking backwards.
* We never invent a field. An event missing a required value is held with the
  question attached, not defaulted.

## What we need from them, and why

1. **`event_id` stable across retries of the same event.** If a retry gets a
   fresh id, the retry becomes a second event. Nothing breaks visibly: the
   booking simply starts carrying a timestamp from the future of its own
   content, and the next genuine update is refused as older. Covered by
   `test_a_retry_that_restamps_the_envelope_is_still_one_event`.
2. **`updated_at` always with an offset.** It is what orders two updates to the
   same booking. Without it, ordering is a guess.
3. **A written list of every value `state` can take.** One value, `pending`,
   has no equivalent in our lifecycle. Today we hold those events. We need to
   know whether `pending` means a booking that may still be confirmed or one
   that was abandoned, because the two answers are opposite.
4. **Confirmation that `amount_minor` is per person for the whole stay.** This
   one is invisible in the data: every payload parses either way, and if the
   assumption is wrong every invoice is wrong by a factor of the party size.

## Open, and since when

The kernel keeps this list itself, in `quarantine_items`, with the date each
question was raised and the sentence to send. The monthly reconciliation prints
it. Nothing is ever dropped quietly, so nobody has to remember.

## The assumption in the money field

`amount_minor` was described to us as **the price of one unit**, so a booking
for two pax is worth twice it. That is a declaration, not something the payload
says.

**The question that would change the behaviour:** is `amount_minor` the price
per pax, or the total for the booking? The two readings differ by the quantity
on every invoice, and both parse.

Read the other feed's note next to this one: it carries the same kind of
integer and was told the opposite thing.
