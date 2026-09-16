"""Where normalisation has to stop.

The two sources here are not two integrations. They are two incompatible
representations crossing the same boundary, and the reason there are two is to
show the line past which making things uniform stops being possible.

Shape can be normalised. One feed pushes JSON with offsets, the other drops a
nightly CSV with naive local times and its own column spellings, and the
adapter absorbs all of it. Meaning cannot. Both feeds carry an integer in minor
units and a quantity, and the integer means a different thing in each. No
payload says which, both readings parse, and the difference is the quantity on
every invoice.

The question that would change the behaviour is written next to each
declaration, in the adapter, where somebody reading the code will meet it.
"""

from __future__ import annotations

import inspect

from sqlalchemy import select

from conftest import PERIOD, coralbay_event, lindhoff_csv, lindhoff_row
from kernel import intake
from kernel.billing import run
from kernel.models import BillingLine, Booking, Entitlement
from kernel.partners import coralbay as coralbay_module
from kernel.partners import lindhoff as lindhoff_module


def test_the_same_number_means_different_money_in_the_two_feeds(session, coralbay, lindhoff):
    """18000 with a quantity of 3 is either 540.00 or 180.00.

    Both are defensible readings of the same payload. Nothing here picks the
    likelier one: it applies what each partner said it meant, and says so.
    """
    intake.apply_delivery(session, coralbay, coralbay_event(
        event_id="e1", booking_ref="CB-8001", amount_minor=18000, pax=3))
    intake.import_file(session, lindhoff, "night.csv", lindhoff_csv(
        [lindhoff_row(external_ref="LH-8001", price_cents=18000, units=3)]))
    session.commit()

    coralbay_booking = session.scalars(
        select(Booking).where(Booking.partner == "coralbay")).one()
    lindhoff_booking = session.scalars(
        select(Booking).where(Booking.partner == "lindhoff")).one()

    assert coralbay_booking.amount_cents == lindhoff_booking.amount_cents == 18000
    assert coralbay_booking.quantity == lindhoff_booking.quantity == 3
    assert coralbay_booking.amount_basis == "per_unit"
    assert lindhoff_booking.amount_basis == "per_booking"
    assert run.billable_amount(coralbay_booking) == 54000
    assert run.billable_amount(lindhoff_booking) == 18000


def test_the_billing_run_carries_the_difference_through(session, coralbay, lindhoff):
    """The interpretation is not a display detail, it is the amount on the line."""
    intake.apply_delivery(session, coralbay, coralbay_event(
        event_id="e1", booking_ref="CB-8002", amount_minor=18000, pax=3))
    intake.import_file(session, lindhoff, "night.csv", lindhoff_csv(
        [lindhoff_row(external_ref="LH-8002", price_cents=18000, units=3)]))
    session.commit()

    run.prepare(session, PERIOD)
    session.commit()

    amounts = dict(
        session.execute(
            select(Booking.partner, BillingLine.amount_cents)
            .join(Entitlement, Entitlement.booking_id == Booking.id)
            .join(BillingLine, BillingLine.entitlement_id == Entitlement.id)
        ).all()
    )
    assert amounts == {"coralbay": 54000, "lindhoff": 18000}


def test_each_declaration_carries_the_question_that_would_change_it():
    """An assumption with no question attached is indistinguishable from a fact."""
    assert coralbay_module.AMOUNT_BASIS == "per_unit"
    assert lindhoff_module.AMOUNT_BASIS == "per_booking"
    assert "is amount_minor the price per pax" in inspect.getsource(coralbay_module)
    assert "does price_cents already include" in inspect.getsource(lindhoff_module)


def test_a_declared_column_spelling_is_accepted_and_an_undeclared_one_stops_the_file(
    session, lindhoff
):
    """Shape, on the other hand, is exactly what an adapter is for.

    Both spellings of the renamed column are in circulation and both are
    written down, so both are read. A third spelling is refused rather than
    matched by similarity, because matching by similarity is how a file gets
    imported into the wrong field for a week.
    """
    header = ["ext_ref", "status", "cust", "article", "units",
              "from", "to", "price_cents", "curr", "changed_at"]
    row = lindhoff_row(external_ref="LH-8003")
    row["ext_ref"] = row["external_ref"]

    accepted = intake.import_file(session, lindhoff, "old-shape.csv",
                                  lindhoff_csv([row], header=header))
    assert accepted.state.value == "applied"

    strange = ["reference"] + header[1:]
    row["reference"] = row["external_ref"]
    refused = intake.import_file(session, lindhoff, "new-shape.csv",
                                 lindhoff_csv([row], header=strange))
    assert refused.state.value == "rejected_incomplete"
    assert "no declared spelling" in refused.reason
