# The trust boundary

Most integration incidents are not caused by a bug. They are caused by a
sentence nobody ever wrote down: what the system guarantees on its own, and
what it can only ever inherit from a partner.

This is that sentence, split in two.

## What the kernel guarantees, whatever the partners do

| Guarantee | Kept by | Proven by |
|---|---|---|
| A booking exists once per partner reference | unique index `uq_booking_identity` | `test_one_booking_per_partner_reference` |
| A partner event is applied once | unique index `uq_processed_event` | `test_one_application_per_partner_event_id` |
| An update never moves a booking backwards | interval ordering in `clock.compare` | `test_out_of_order_update_does_not_move_the_booking_backwards` |
| Two versions with one timestamp are never silently merged | conflict path in `intake.service` | `test_same_timestamp_and_different_content_is_a_conflict_not_an_overwrite` |
| Nothing is defaulted, ever | adapters return a rejection, never a value | `test_an_unknown_status_value_is_held_not_mapped_to_the_nearest_one` |
| A file is applied whole or not at all | structural checks in `import_file` | `test_a_truncated_file_applies_nothing_at_all` |
| A claim link is spent once | conditional update in one statement | `test_two_people_opening_the_same_claim_link_at_once_do_not_both_get_in` |
| Sharing stops at the bound | unique slot plus check constraint | `test_a_fourth_share_cannot_exist_even_in_raw_sql` |
| One billing line per entitlement per period | unique index `uq_line_per_entitlement_period` | `test_a_second_line_for_the_same_entitlement_and_period_cannot_exist` |
| One charge per line, through a crash | derived idempotency key plus a commit per line | `test_a_charge_whose_answer_was_lost_is_not_taken_a_second_time` |
| Any delivery order converges on the same state | all of the above together | `test_any_order_and_any_number_of_redeliveries_reach_the_same_state` |

Five of those eleven are kept by the database rather than by the service layer.
That is deliberate. Service code is the part a future change can walk around by
accident; a unique index is not.

## What only the partner can confirm

No amount of code on our side turns these into guarantees. Each one is a
sentence somebody has to get in writing, and each one is cheap to get before
the integration exists and expensive afterwards.

| Assumption | If it is wrong | How the kernel behaves meanwhile |
|---|---|---|
| The external reference is stable for the life of a booking | every rename becomes a new booking and a second entitlement | nothing detects it, this is the one to confirm first |
| The event id is stable across retries | retries become new events, the booking's clock runs ahead of its content, later updates are refused as older and nobody is told | dedup by event id, so it is correct while the assumption holds |
| The status vocabulary is complete | an unknown status is a booking in a state we cannot reason about | held with the question attached, never mapped to the nearest known value |
| Naive timestamps are in the declared zone | ordering is wrong by an hour twice a year, on the two busiest nights of the year for changes | treated as a one hour window, refused when the windows overlap |
| A money field is per unit rather than per booking | every invoice is wrong by the quantity, and every payload still parses | assumed per unit and written down here, which is the only honest option |
| The nightly file is complete | a truncated file cancels everything it failed to mention | structural checks plus a floor taken from this feed's own history |

## Why the list is the deliverable

Half the calendar time of an integration goes on getting answers to the right
hand column. An engineer who starts that conversation in week one, with the
exact sentence to send and the date it was first asked, gets a different answer
than one who starts it in week five, when the thing is late and the question
sounds like an excuse.

The kernel keeps that list itself, in `quarantine_items`, with the date each
question was raised. The monthly reconciliation prints it. Nothing has to be
remembered by a person.
