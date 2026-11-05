ALTER TABLE appointment_notifications
ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_appointment_notifications_claimed_at
ON appointment_notifications (status, claimed_at);