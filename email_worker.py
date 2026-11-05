
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from database import SessionLocal
from models import Appointment, Lead, AppointmentNotification
from email_service import send_appointment_email

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BATCH_SIZE = 20


def process_appointment_notifications():
    """Send due appointment emails from the persistent notification queue."""

    db = SessionLocal()

    try:
        # Recover notifications stuck in sending after a worker crash.
        stale_before = datetime.now(timezone.utc) - timedelta(minutes=15)

        db.execute(
            text("""
                UPDATE appointment_notifications
                SET status = 'pending',
                    claimed_at = NULL,
                    scheduled_at = :now,
                    last_error = 'Recovered after worker timeout.'
                WHERE status = 'sending'
                  AND (claimed_at IS NULL OR claimed_at < :stale_before)
                  AND attempts < :max_attempts
            """),
            {
                "now": datetime.now(timezone.utc),
                "stale_before": stale_before,
                "max_attempts": MAX_ATTEMPTS,
            },
        )

        # Permanently fail notifications that exhausted their attempts.
        db.execute(
            text("""
                UPDATE appointment_notifications
                SET status = 'failed',
                    claimed_at = NULL,
                    last_error = 'Maximum attempts reached after worker timeout.'
                WHERE status = 'sending'
                  AND claimed_at < :stale_before
                  AND attempts >= :max_attempts
            """),
            {
                "stale_before": stale_before,
                "max_attempts": MAX_ATTEMPTS,
            },
        )

        db.commit()
        # Claim due notifications without allowing another worker
        # to claim the same rows simultaneously.
        claimed = db.execute(
            text("""
                SELECT id
                FROM appointment_notifications
                WHERE status = 'pending'
                  AND scheduled_at <= :now
                  AND attempts < :max_attempts
                ORDER BY scheduled_at
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            """),
            {
                "now": datetime.now(timezone.utc),
                "max_attempts": MAX_ATTEMPTS,
                "batch_size": BATCH_SIZE,
            },
        ).fetchall()

        notification_ids = [row[0] for row in claimed]

        if not notification_ids:
            db.rollback()
            return

        db.execute(
            text("""
                UPDATE appointment_notifications
                SET status = 'sending',
                    attempts = attempts + 1,
                    claimed_at = :claimed_at
                WHERE id = ANY(:ids)
            """),
{
    "ids": notification_ids,
    "claimed_at": datetime.now(timezone.utc),
},
        )

        db.commit()

        # Process each claimed notification separately.
        for notification_id in notification_ids:
            _send_one_notification(notification_id)

    except Exception:
        db.rollback()
        logger.exception("Appointment notification worker failed.")

    finally:
        db.close()


def _send_one_notification(notification_id: int):
    """Send one notification and persist its outcome."""

    db = SessionLocal()

    try:
        notification = db.get(
            AppointmentNotification,
            notification_id,
        )

        if not notification or notification.status != "sending":
            return

        appointment = db.get(
            Appointment,
            notification.appointment_id,
        )

        if not appointment:
            notification.status = "cancelled"
            notification.claimed_at = None
            db.commit()
            return

        # Never send an email for a cancelled or unconfirmed booking.
        if (
            appointment.status != "confirmed"
            or appointment.booking_state != "confirmed"
        ):
            notification.status = "cancelled"
            notification.claimed_at = None
            db.commit()
            return

        now = datetime.now(timezone.utc)

        # Do not send reminders for appointments that have passed.
        if (
            notification.notification_type == "reminder_24h"
            and appointment.start_at <= now
        ):
            notification.status = "cancelled"
            notification.claimed_at = None
            db.commit()
            return

        lead = db.get(Lead, appointment.lead_id)

        if not lead or not lead.email or not lead.email.strip():
            notification.status = "failed"
            notification.claimed_at = None
            notification.last_error = "Patient email address is missing."
            db.commit()
            return

        # Send the email.
        send_appointment_email(
            to_email=lead.email.strip(),
            patient_name=lead.name,
            service=appointment.service,
            start_at=appointment.start_at,
            notification_type=notification.notification_type,
        )

        notification.status = "sent"
        notification.claimed_at = None
        notification.sent_at = datetime.now(timezone.utc)
        notification.last_error = None
        db.commit()

        logger.info(
            "Appointment notification %s sent for appointment %s.",
            notification.id,
            appointment.id,
        )

    except Exception as exc:
        db.rollback()

        # Re-open the notification to persist the failure.
        notification = db.get(
            AppointmentNotification,
            notification_id,
        )

        if notification:
            notification.last_error = str(exc)[:2000]
            notification.claimed_at = None

            if notification.attempts >= MAX_ATTEMPTS:
                notification.status = "failed"
            else:
                notification.status = "pending"
                notification.scheduled_at = (
                    datetime.now(timezone.utc)
                    + timedelta(minutes=5 * notification.attempts)
                )

            db.commit()

        logger.exception(
            "Failed to send appointment notification %s.",
            notification_id,
        )

    finally:
        db.close()

