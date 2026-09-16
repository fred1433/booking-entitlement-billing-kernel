"""The edges, one at a time.

Every test in this file is a thing that goes wrong once real partner traffic
arrives. Each one fails without the control it names and passes with it, and
the name of the control is in the assertion, not in a comment.
"""

from __future__ import annotations

from sqlalchemy import select

from conftest import coralbay_event, lindhoff_csv, lindhoff_row
from kernel.intake import apply_delivery, expected_minimum_rows, import_file, run_pull_window
from kernel.models import (
    BatchState,
    Booking,
    Delivery,
    DeliveryOutcome,
    JournalEntry,
    ProcessedEvent,
    QuarantineItem,
)


def _booking(session, partner, external_id):
    return session.execute(
        select(Booking).where(Booking.partner == partner, Booking.external_id == external_id)
    ).scalar_one()


def test_duplicate_delivery_is_a_no_op(session, coralbay):
    """A partner that delivers at least once delivers twice. The second time changes nothing.

    Without the unique index on (partner, event id), the second copy is applied
    again: harmless today, and the reason an entitlement gets re-created after
    it was cancelled the moment the two copies straddle a cancellation.
    """
    event = coralbay_event(event_id="evt-1", booking_ref="CB-1001")

    first = apply_delivery(session, coralbay, event)
    second = apply_delivery(session, coralbay, event)
    session.commit()

    assert first.outcome is DeliveryOutcome.APPLIED
    assert second.outcome is DeliveryOutcome.DUPLICATE
    assert session.execute(select(ProcessedEvent)).scalars().all().__len__() == 1
    # Both arrivals are in the journal of deliveries: "did you receive it" and
    # "did you act on it" are different questions when a partner is asking.
    assert session.execute(select(Delivery)).scalars().all().__len__() == 2


def test_out_of_order_update_does_not_move_the_booking_backwards(session, coralbay):
    """The older copy of an update must not undo the newer one.

    Two webhooks cross in flight, or a retry of an old event lands after a new
    one. Applying by arrival order silently reinstates a cancelled booking.
    """
    apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-new", booking_ref="CB-2001", state="cancelled",
        updated_at="2026-08-04T12:00:00+02:00"))
    late = apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-old", booking_ref="CB-2001", state="confirmed",
        updated_at="2026-08-04T09:00:00+02:00"))
    session.commit()

    assert late.outcome is DeliveryOutcome.SUPERSEDED
    assert _booking(session, "coralbay", "CB-2001").status == "cancelled"
    refusal = session.execute(
        select(JournalEntry).where(JournalEntry.outcome == "refused")
    ).scalars().first()
    assert "backwards" in refusal.reason


def test_same_timestamp_and_same_content_is_a_duplicate(session, lindhoff):
    """The same row in two nightly files is a duplicate, not an update.

    Partner B sends no event id at all, so content is the only thing that can
    tell a redelivery from a change.
    """
    row = lindhoff_row(external_ref="LH-3001")
    apply_delivery(session, lindhoff, row, channel="file")
    again = apply_delivery(session, lindhoff, row, channel="file")
    session.commit()

    assert again.outcome is DeliveryOutcome.DUPLICATE
    assert again.reason == "same timestamp, same content"


def test_a_retry_that_restamps_the_envelope_is_still_one_event(session, coralbay):
    """A retry carrying a fresher timestamp must not advance the booking's clock.

    This is the quiet one. If the retry is treated as a new update, the booking
    now holds a timestamp from the future of its own content, and the genuine
    update that arrives a minute later is refused as older. The booking then
    stops receiving updates, and nothing alerts.
    """
    apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-7", booking_ref="CB-4001", updated_at="2026-08-04T09:00:00+02:00"))
    retry = apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-7", booking_ref="CB-4001", updated_at="2026-08-04T09:05:00+02:00"))
    genuine = apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-8", booking_ref="CB-4001", state="amended",
        updated_at="2026-08-04T09:02:00+02:00"))
    session.commit()

    assert retry.outcome is DeliveryOutcome.DUPLICATE
    booking = _booking(session, "coralbay", "CB-4001")
    assert booking.updated_at_raw == "2026-08-04T09:02:00+02:00"
    assert genuine.outcome is DeliveryOutcome.APPLIED
    assert booking.status == "amended"


def test_an_update_that_changes_nothing_is_applied_and_said_so(session, coralbay):
    """Most of a nightly feed changes nothing. Knowing how much is a lever.

    "You sent four thousand updates and twelve of them changed something" is a
    sentence that shortens a partner call, and it costs one fingerprint.
    """
    apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-a", booking_ref="CB-5001", updated_at="2026-08-04T09:00:00+02:00"))
    quiet = apply_delivery(session, coralbay, coralbay_event(
        event_id="evt-b", booking_ref="CB-5001", updated_at="2026-08-04T10:00:00+02:00"))
    session.commit()

    assert quiet.outcome is DeliveryOutcome.APPLIED
    assert quiet.reason == "no business change, only a newer timestamp"


def test_an_unknown_status_value_is_held_not_mapped_to_the_nearest_one(session, lindhoff):
    """A status we have never been told about is a question, not a default.

    Mapping an unknown value to the closest known one is how an entitlement
    gets activated for a booking that was put on hold.
    """
    result = apply_delivery(session, lindhoff, lindhoff_row(
        external_ref="LH-6001", status="HOLD"), channel="file")
    session.commit()

    assert result.outcome is DeliveryOutcome.QUARANTINED
    item = session.execute(select(QuarantineItem)).scalar_one()
    assert item.kind == "unknown_status_value"
    assert "'HOLD'" in item.needed_from_partner
    assert session.execute(select(Booking)).scalars().all() == []


def test_a_missing_field_is_held_and_the_question_is_written_for_the_partner(session, coralbay):
    """Nothing is defaulted. The quarantine row is the message to send."""
    result = apply_delivery(session, coralbay, coralbay_event(event_id="evt-x", currency=""))
    session.commit()

    assert result.outcome is DeliveryOutcome.QUARANTINED
    item = session.execute(select(QuarantineItem)).scalar_one()
    assert item.kind == "missing_field"
    assert "currency" in item.detail


def test_a_quarantined_event_is_not_marked_as_processed(session, coralbay):
    """When the partner resends the fixed payload, we look again.

    A quarantined event is an open question with them. Marking its id as
    processed would swallow the answer.
    """
    apply_delivery(session, coralbay, coralbay_event(event_id="evt-q", state="pending"))
    fixed = apply_delivery(session, coralbay, coralbay_event(event_id="evt-q", state="confirmed"))
    session.commit()

    assert fixed.outcome is DeliveryOutcome.APPLIED


def test_a_renamed_column_is_read_only_under_a_spelling_the_partner_declared(session, lindhoff):
    """Both agreed spellings work. A third one stops the file.

    Column renames happen mid life. Accepting the two spellings we were told
    about is correct; guessing at a third by similarity is how a file gets
    imported into the wrong field for a week.
    """
    old_header = ["ext_ref", "status", "cust", "article", "units",
                  "from", "to", "price_cents", "curr", "changed_at"]
    rows = [{**lindhoff_row(external_ref="LH-7001"), "ext_ref": "LH-7001"}]
    accepted = import_file(session, lindhoff, "old.csv", lindhoff_csv(rows, header=old_header))

    unknown_header = ["externalRef", "status", "cust", "article", "units",
                      "from", "to", "price_cents", "curr", "changed_at"]
    rows2 = [{**lindhoff_row(external_ref="LH-7002"), "externalRef": "LH-7002"}]
    refused = import_file(session, lindhoff, "new.csv", lindhoff_csv(rows2, header=unknown_header))
    session.commit()

    assert accepted.state is BatchState.APPLIED and accepted.applied == 1
    assert refused.state is BatchState.REJECTED_INCOMPLETE
    assert "external_ref" in refused.reason


def test_two_readings_inside_the_repeated_hour_cannot_be_ordered(session, lindhoff):
    """One hour a year, a naive local time is two instants an hour apart.

    On the night the clocks go back, 02:30 and 02:45 local are not necessarily
    in that order: either can be on either side of the change. The kernel
    refuses to pick, raises it with the partner, and resumes as soon as a
    reading is unambiguous again.
    """
    first = apply_delivery(session, lindhoff, lindhoff_row(
        external_ref="LH-8001", changed_at="2026-10-25 02:30:00", price_cents=18000),
        channel="file")
    unorderable = apply_delivery(session, lindhoff, lindhoff_row(
        external_ref="LH-8001", changed_at="2026-10-25 02:45:00", price_cents=19000),
        channel="file")
    later = apply_delivery(session, lindhoff, lindhoff_row(
        external_ref="LH-8001", changed_at="2026-10-25 04:00:00", price_cents=20000),
        channel="file")
    session.commit()

    assert first.outcome is DeliveryOutcome.APPLIED
    assert unorderable.outcome is DeliveryOutcome.ORDER_NOT_PROVABLE
    assert later.outcome is DeliveryOutcome.APPLIED
    booking = _booking(session, "lindhoff", "LH-8001")
    assert booking.amount_cents == 20000
    item = session.execute(
        select(QuarantineItem).where(QuarantineItem.kind == "order_not_provable")
    ).scalar_one()
    assert "explicit offset" in item.needed_from_partner


def test_a_local_time_that_never_happened_is_held(session, lindhoff):
    """In spring the clock jumps an hour, and some rows carry a reading from inside the jump."""
    result = apply_delivery(session, lindhoff, lindhoff_row(
        external_ref="LH-8002", changed_at="2026-03-29 02:30:00"), channel="file")
    session.commit()

    assert result.outcome is DeliveryOutcome.QUARANTINED
    item = session.execute(select(QuarantineItem)).scalar_one()
    assert item.kind == "timestamp_nonexistent_local_time"


def test_overlapping_pull_windows_change_nothing_the_second_time(session, coralbay):
    """Pull windows have to overlap, or they lose whatever was written in the gap.

    Overlap is only free if reprocessing is free, which is what every rule
    above is for.
    """
    window = [
        coralbay_event(event_id="evt-p1", booking_ref="CB-9001",
                       updated_at="2026-08-04T09:00:00+02:00"),
        coralbay_event(event_id="evt-p2", booking_ref="CB-9002",
                       updated_at="2026-08-04T09:30:00+02:00"),
    ]
    run_pull_window(session, coralbay, window)
    second = run_pull_window(session, coralbay, window)
    session.commit()

    assert [result.outcome for result in second] == [DeliveryOutcome.DUPLICATE] * 2
    assert len(session.execute(select(Booking)).scalars().all()) == 2


def test_a_truncated_file_applies_nothing_at_all(session, lindhoff):
    """Half a nightly file is worse than no file.

    Applying the rows that arrived and leaving the rest at yesterday's state
    produces a database that is internally consistent and wrong, with nothing
    to show which half is which.
    """
    rows = [lindhoff_row(external_ref=f"LH-90{index:02d}") for index in range(5)]
    good = lindhoff_csv(rows)
    truncated = good[: int(len(good) * 0.7)]

    result = import_file(session, lindhoff, "nightly.csv", truncated)
    session.commit()

    assert result.state is BatchState.REJECTED_INCOMPLETE
    assert "truncated" in result.reason
    assert session.execute(select(Booking)).scalars().all() == []


def test_a_declared_row_count_that_does_not_match_stops_the_file(session, lindhoff):
    """When the partner tells us how many rows to expect, we check."""
    rows = [lindhoff_row(external_ref=f"LH-91{index:02d}") for index in range(3)]
    result = import_file(session, lindhoff, "nightly.csv", lindhoff_csv(rows, trailer=5))
    session.commit()

    assert result.state is BatchState.REJECTED_INCOMPLETE
    assert "declares 5 rows" in result.reason


def test_a_file_far_shorter_than_this_feed_is_held_for_a_human(session, lindhoff):
    """A short file and a quiet night look the same. Only history separates them."""
    for night in range(3):
        rows = [lindhoff_row(external_ref=f"LH-92{night}{index:02d}",
                             changed_at=f"2026-08-0{night + 1} 02:00:00")
                for index in range(10)]
        import_file(session, lindhoff, f"night{night}.csv", lindhoff_csv(rows))
    session.commit()

    floor = expected_minimum_rows(session, lindhoff.name)
    result = import_file(session, lindhoff, "night-short.csv",
                         lindhoff_csv([lindhoff_row(external_ref="LH-9300")]),
                         min_expected_rows=floor)
    session.commit()

    assert floor == 2
    assert result.state is BatchState.HELD_FOR_REVIEW
    assert result.applied == 0


def test_one_bad_row_does_not_hold_a_good_night_hostage(session, lindhoff):
    """Structure stops a batch. A single unreadable row does not."""
    rows = [lindhoff_row(external_ref="LH-9401"),
            lindhoff_row(external_ref="LH-9402", status="HOLD"),
            lindhoff_row(external_ref="LH-9403")]
    result = import_file(session, lindhoff, "nightly.csv", lindhoff_csv(rows))
    session.commit()

    assert result.state is BatchState.APPLIED
    assert (result.applied, result.quarantined) == (2, 1)
