# Lindhoff Booking Suite, integration note

**Lindhoff is a fictional partner.** It exists in this repository only, and it
was invented to behave the way file based booking partners behave.

## How they reach us

One CSV per night, dropped in a location we poll. Header row, comma separated,
optionally followed by a `#rows=N` trailer.

```
external_ref,status,cust,article,units,from,to,price_cents,curr,changed_at
LH-5001,OK,cust-31,CITY-PASS-72H,1,2026-08-10,2026-08-13,18000,EUR,2026-08-04 09:15:00
#rows=1
```

`status` is one of `OK`, `CHG`, `VOID`. `external_ref` is also accepted spelled
`ext_ref`, because that is what the older files use and both spellings were
confirmed in writing. A third spelling stops the file rather than being matched
by similarity.

## How the file is applied

A file is applied as a whole or not at all. Four things stop it:

* a column with no declared spelling;
* a row whose field count does not match the header, or a last line with no
  terminator, which is what a truncated download looks like;
* a `#rows=N` trailer that does not match what the file carries;
* a file far shorter than this feed's own recent history. A short file and a
  quiet night are indistinguishable from the outside, and only one of them is
  safe to apply. Applying a truncated file silently cancels every booking it
  failed to mention.

A single unreadable row does not stop the file. It is quarantined with the
question attached, and the rest of the night is applied. Holding four thousand
good rows hostage to one bad one is its own outage.

## The one that costs the most: naive local timestamps

`changed_at` is written as local wall clock time in Europe/Amsterdam, with no
offset. That is fine for three hundred and sixty four days a year.

On the night the clocks go back, every reading between 02:00 and 03:00 happens
twice, one hour apart, and nothing in the row says which. Two rows for the same
booking, at 02:30 and 02:45, are not necessarily in that order. The kernel
therefore treats such a reading as a one hour window rather than an instant,
and it applies an update only when its window is entirely after the one it
holds. When the windows overlap, it changes nothing, raises the question, and
resumes as soon as a reading is unambiguous again.

On the night the clocks go forward, an hour does not exist. Rows carrying a
reading from inside that hour are held: that wall clock time never happened.

Both are covered by `test_two_readings_inside_the_repeated_hour_cannot_be_ordered`
and `test_a_local_time_that_never_happened_is_held`.

**What we need:** `changed_at` in UTC, or with an explicit offset. It is a one
line change on their side and it removes this entire class of problem for good.
Until then the kernel is correct and slightly slower, which is the right way
round.

## What else we need

1. **A row identifier that is stable for the life of a booking.** Everything
   the kernel does rests on `external_ref` meaning the same booking tomorrow.
2. **The full list of values `status` can take.** Rows carrying anything
   outside `OK`, `CHG`, `VOID` are held, not mapped to the nearest known value.
   Mapping an unknown status to the closest one is how an entitlement gets
   activated for a booking that was put on hold.
3. **A `#rows=N` trailer on every file.** It turns a truncated download from
   something we infer into something we detect.
4. **Confirmation that `price_cents` is per unit.** Same trap as everywhere
   else: the file parses either way and the invoice is wrong by the number of
   units.

## The assumption in the money field

`price_cents` was described to us as **the total for the row**, already
multiplied by `units`. That is a declaration, not something the payload says,
and it is deliberately the opposite of what the other feed was told.

**The question that would change the behaviour:** does `price_cents` already
include `units`, or is it the price of one? Read it the wrong way and every
invoice from this feed is wrong by the quantity, and every row still parses.
