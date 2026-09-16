"""booking-to-billing integration kernel.

A small, readable core for the part of a booking integration that is not the
domain: duplicate deliveries, out-of-order updates, partner data that disagrees
with itself, and a billing run that must never charge twice.

Both partner adapters in this repository are fictional. They are written to
disagree with each other on purpose.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
