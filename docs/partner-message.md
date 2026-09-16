# The message somebody actually has to send

This is a **synthetic example**. It was not sent to anybody, and neither partner
in this repository exists.

It is here because the partner-facing half of an integration role is usually
invisible in a codebase, and it is where a good part of the calendar time goes.
A quarantine entry whose `needed_from_partner` field is an internal error
message is an entry that will be rewritten by hand before anybody can send it,
which means it will not be sent.

## The message

> These two records have the same booking ID and updatedAt but different
> statuses. Can updatedAt identify a revision, or is there a separate sequence?
> The affected booking remains on billing hold until this is resolved.

Three properties worth copying, whatever the integration:

* it states the observation before the request, so the reader can check it;
* it asks a question with two concrete answers rather than "please advise";
* it says what the consequence currently is, which is what makes it urgent for
  them rather than only for us.

That exact sentence is generated, not written by hand. It is the
`question_for_partner` on the conflict row, it is asserted in
`tests/test_conflict_and_quarantine.py::test_same_identity_same_version_different_content_is_kept_not_resolved`,
and it is what the reconciliation prints.

## The line that goes next to it

| Field | Value |
|---|---|
| Expected data | whether `updatedAt` identifies a revision, or a separate sequence exists |
| Counterpart | the partner's integration contact |
| Consequence | one booking is on billing hold and is not prepared |
| Next action | ask, then replay the held message |
| Status | **Not requested, synthetic example** |

No follow up dates appear anywhere in this repository. A tracking table with
invented dates in it looks exactly like a real one, which is the problem.
