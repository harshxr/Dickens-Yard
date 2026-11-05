# Appointment endpoint behavior

- `POST /api/v1/appointments/book` is the voice-agent booking workflow. It
  reserves a slot, writes a Google Calendar event, and records the confirmed
  booking. New callers send an `Idempotency-Key` header. Older callers without
  the header receive a stable key derived from their normalized request body.
- `POST /api/v1/appointments` is the existing database-only appointment
  creation endpoint. It does not write to Google Calendar. No in-repository
  caller currently uses it, but external consumers have not been inventoried;
  retain its behavior until those consumers are identified.

# Phase 4 schema migration and recovery

Before deploying code that uses persisted booking keys, apply
`migrations/2026_11_01_booking_idempotency.psql` to the target PostgreSQL
database, deploy the backend, then deploy the updated agent. Do not run the
updated agent against a backend that does not persist the key. The migration
adds nullable idempotency, request-hash, and Calendar
reference fields. Existing appointment rows remain in place and are marked
`booking_state = 'confirmed'`; their idempotency keys remain NULL. The unique
index allows those existing rows and enforces one key per business for new
bookings. The migration is idempotent, but should be run from a reviewed,
backed-up deployment procedure. Application startup `create_all()` does not
apply this migration to existing tables.

The booking API persists a `pending` appointment before calling Google
Calendar. Its deterministic Calendar event ID is derived from the business
and idempotency key. If a process stops after Google accepts an event, a retry
with the same key retrieves that same event ID and marks the appointment
confirmed. If Calendar's outcome is uncertain or final DB confirmation fails,
the row remains pending and blocks the slot. Do not delete such rows; retry
with the same key after Calendar is reachable, or reconcile the stored
appointment and event IDs with the practice team. Logs identify a booking by
database appointment ID and do not include caller details, email, phone,
refresh tokens, or Calendar credentials.

PostgreSQL and Google Calendar do not share a transaction. The durable pending
row, deterministic Calendar event ID, and same-key retry make failures
recoverable and prevent ordinary retries from inserting another event; they
do not provide a cross-service exactly-once transaction. There is no automatic
background reconciler in this phase, so unresolved pending rows require the
caller to retry the same request or staff to initiate reconciliation.

Staff can locate work requiring review without selecting patient details with:

```sql
SELECT id, business_id, booking_state, calendar_event_id
FROM appointments
WHERE booking_state IN ('calendar_pending', 'reconciliation_required')
ORDER BY id;
```

Retry the original booking with the same `Idempotency-Key` and unchanged
payload. Do not delete a pending reservation or create a replacement key for
it; both actions can make a previously accepted Calendar event harder to
match.

The database-only endpoint remains compatible with legacy appointment rows
and does not participate in Google Calendar reconciliation.
