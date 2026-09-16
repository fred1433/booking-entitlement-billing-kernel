"""Two ways a record stops being decidable, and what billing does about it.

Neither of these is a crash. Both are cases where the data is fine, the code is
fine, and nobody can say what is true. The interesting question is not how they
are detected but what the next billing run does when it meets one.

The policy demonstrated here is conservative: an undecidable booking is not
prepared. That is a demonstration choice, not a recommendation. A real product
might well bill the last confirmed state and correct afterwards. What should
not happen is for the choice to be made by accident, by whichever branch
happened to be written first.
"""

from __future__ import annotations

from sqlalchemy import select

from conftest import PERIOD, coralbay_event, lindhoff_csv, lindhoff_row
from kernel import intake
from kernel.billing import reconciliation, run
from kernel.models import (
    BillingLine,
    Booking,
    DeliveryOutcome,
    LineState,
    QuarantineItem,
    VersionConflict,
)


# --------------------------------------------------------------------------- #
# Same identity, same version, different content
# --------------------------------------------------------------------------- #

def test_same_identity_same_version_different_content_is_kept_not_resolved(session, coralbay):
    """Last write wins is a coin toss, and the loser is invisible.

    Nothing here picks a winner, because nothing here can: two payloads with the
    same ``updated_at`` and different statuses do not say which one was sent
    last. Both are kept, the held state is unchanged, and the question that
    would settle it is written down.
    """
    stamp = "2026-08-04T09:15:00+02:00"
    intake.apply_delivery(session, coralbay, coralbay_event(
        event_id="e1", booking_ref="CB-4001", updated_at=stamp, pax=2))
    result = intake.apply_delivery(session, coralbay, coralbay_event(
        event_id="e2", booking_ref="CB-4001", updated_at=stamp, pax=5))
    session.flush()

    assert result.outcome is DeliveryOutcome.CONFLICT
    booking = session.scalars(select(Booking)).one()
    assert booking.quantity == 2, "the held version was not overwritten"

    conflict = session.scalars(select(VersionConflict)).one()
    assert conflict.resolved_at is None
    assert "same booking ID and updatedAt but different statuses" in conflict.question_for_partner


def test_a_conflict_puts_the_booking_on_hold_for_billing(session, coralbay):
    """The conflict is not a filing cabinet entry, it changes what is billed."""
    stamp = "2026-08-04T09:15:00+02:00"
    intake.apply_delivery(session, coralbay, coralbay_event(
        event_id="e1", booking_ref="CB-4002", updated_at=stamp, pax=2))
    intake.apply_delivery(session, coralbay, coralbay_event(
        event_id="e2", booking_ref="CB-4002", updated_at=stamp, pax=5))
    session.commit()

    run.prepare(session, PERIOD)
    session.commit()

    line = session.scalars(select(BillingLine)).one()
    assert line.state == LineState.BLOCKED.value
    assert line.reason == "conflicting_versions"


def test_answering_the_question_releases_the_hold_and_the_run_bills_it(session, coralbay):
    """The other half of holding something back, and the half usually missing.

    A quarantine that can only be emptied by hand is a quarantine that fills up
    until somebody turns the check off.
    """
    stamp = "2026-08-04T09:15:00+02:00"
    intake.apply_delivery(session, coralbay, coralbay_event(
        event_id="e1", booking_ref="CB-4003", updated_at=stamp, pax=2))
    intake.apply_delivery(session, coralbay, coralbay_event(
        event_id="e2", booking_ref="CB-4003", updated_at=stamp, pax=5))
    session.commit()
    run.prepare(session, PERIOD)
    session.commit()
    assert session.scalars(select(BillingLine)).one().state == LineState.BLOCKED.value

    intake.resolve_and_replay(
        session, coralbay, "coralbay", "CB-4003",
        answer="updatedAt is the revision; the pax=5 record is the later one",
        replay=coralbay_event(event_id="e3", booking_ref="CB-4003",
                              updated_at="2026-08-04T09:20:00+02:00", pax=5),
    )
    session.commit()

    booking = session.scalars(select(Booking)).one()
    assert booking.undecided_reason is None
    assert booking.quantity == 5

    # The line for this period already exists, so it is corrected in place
    # rather than duplicated.
    run.prepare(session, PERIOD)
    session.commit()
    assert session.scalars(select(BillingLine)).one().state == LineState.BLOCKED.value


# --------------------------------------------------------------------------- #
# An unknown value about a booking that is live and billable
# --------------------------------------------------------------------------- #

def test_an_unknown_status_about_a_live_booking_suspends_its_billing(session, lindhoff):
    """Mapping an unknown status to the nearest known one is how a held booking gets billed.

    The row is quarantined with the sentence to send, and the booking it refers
    to stops being prepared, because something was said about it that nobody can
    read.
    """
    intake.import_file(session, lindhoff, "night-1.csv",
                       lindhoff_csv([lindhoff_row(external_ref="LH-6001", status="OK")]))
    session.commit()
    assert session.scalars(select(Booking)).one().undecided_reason is None

    result = intake.apply_delivery(
        session, lindhoff, lindhoff_row(external_ref="LH-6001", status="HOLD"), channel="file")
    session.commit()

    assert result.outcome is DeliveryOutcome.QUARANTINED
    booking = session.scalars(select(Booking)).one()
    assert booking.undecided_reason == "unknown_status_value"

    run.prepare(session, PERIOD)
    session.commit()
    line = session.scalars(select(BillingLine)).one()
    assert line.state == LineState.BLOCKED.value


def test_the_held_message_is_not_marked_processed_so_a_correction_is_looked_at(session, lindhoff):
    """A held event id stays unclaimed on purpose.

    When the partner resends the corrected payload, it has to be looked at
    again rather than swallowed as a repeat of something already handled.
    """
    intake.apply_delivery(session, lindhoff, lindhoff_row(external_ref="LH-6002", status="HOLD"))
    session.commit()

    item = session.scalars(select(QuarantineItem)).one()
    assert item.resolved_at is None
    assert "not one of OK, CHG, VOID" in item.detail

    corrected = intake.apply_delivery(
        session, lindhoff, lindhoff_row(external_ref="LH-6002", status="OK"))
    session.commit()
    assert corrected.outcome is DeliveryOutcome.APPLIED


def test_the_report_says_what_is_open_and_what_it_is_holding_up(session, lindhoff):
    """The blocked list, generated rather than remembered."""
    intake.import_file(session, lindhoff, "night-1.csv",
                       lindhoff_csv([lindhoff_row(external_ref="LH-6003", status="OK")]))
    intake.apply_delivery(session, lindhoff, lindhoff_row(external_ref="LH-6003", status="HOLD"))
    session.commit()
    run.prepare(session, PERIOD)
    session.commit()

    report = reconciliation.reconcile(session, PERIOD)
    rendered = reconciliation.render_reconciliation(session, report)

    assert len(report.questions) == 1
    assert report.questions[0].holding_up == "billing preparation for this booking"
    assert "Not requested, synthetic example" in rendered
    assert "blocked" in rendered
