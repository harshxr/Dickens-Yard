
# pyright: reportMissingImports=false
# pyright: reportMissingImports=false
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
)

from database import Base

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    phone = Column(String(20), nullable=False)
    email = Column(String(255), nullable=True)
    interest = Column(String(200), nullable=False)
    status = Column(String(20), nullable=False, default="new")


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (

        UniqueConstraint(
            "business_id",
            "idempotency_key",
            name="uq_appointments_business_idempotency_key",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(String(100), nullable=False, index=True)
    lead_id = Column(
        Integer,
        ForeignKey("leads.id"),
        nullable=False,
        index=True,
    )
    service = Column(String(200), nullable=False)
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default="confirmed")
    notes = Column(Text, nullable=True)
    # Nullable to preserve rows created before persisted booking idempotency.
    idempotency_key = Column(String(128), nullable=True)
    request_hash = Column(String(64), nullable=True)
    booking_state = Column(
        String(32), nullable=False, default="confirmed", server_default="confirmed"
    )
    calendar_event_id = Column(String(255), nullable=True)
    calendar_event_link = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class CallbackTask(Base):
    __tablename__ = "callback_tasks"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(String(100), nullable=False, index=True)
    lead_id = Column(
        Integer,
        ForeignKey("leads.id"),
        nullable=False,
        index=True,
    )
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default="scheduled")
    assigned_to = Column(String(150), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
    )
class GoogleCalendarConnection(Base):
    __tablename__ = "google_calendar_connections"

    id = Column(Integer, primary_key=True, index=True)

    business_id = Column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
    )

    calendar_id = Column(
        String(255),
        nullable=False,
        default="primary",
    )

    encrypted_refresh_token = Column(Text, nullable=False)

    connected = Column(Boolean, nullable=False, default=True)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
    )

    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

class OutboundCall(Base):
    __tablename__ = "outbound_calls"
    __table_args__ = (
        Index(
            "ix_outbound_calls_business_request_created",
            "business_id",
            "request_hash",
            "created_at",
        ),
        UniqueConstraint(
            "business_id",
            "idempotency_key_hash",
            name="uq_outbound_calls_business_idempotency_hash",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(String(100), nullable=True, index=True)
    lead_id = Column(
        Integer, ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    idempotency_key_hash = Column(String(64), nullable=True)
    request_hash = Column(String(64), nullable=True)
    purpose = Column(String(200), nullable=True)
    caller_id = Column(String(20), nullable=True)
    consent_attestation_id = Column(String(200), nullable=True)
    requested_by = Column(String(100), nullable=True)
    room_name = Column(String(150), nullable=False, unique=True, index=True)
    sip_call_id = Column(String(150), nullable=True, index=True)
    phone_masked = Column(String(30), nullable=False)
    phone_hash = Column(String(64), nullable=False, index=True)
    status = Column(String(30), nullable=False, default="initiated")
    consent = Column(Boolean, nullable=False, default=False)
    consent_at = Column(DateTime, nullable=False)
    source = Column(String(100), nullable=False, default="nivakya-website")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
class ProcessedWebhookEvent(Base):
    __tablename__ = "processed_webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String(255), nullable=False, unique=True, index=True)
    event_type = Column(String(100), nullable=False)
    room_name = Column(String(150), nullable=True, index=True)
    received_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
class AppointmentNotification(Base):
    __tablename__ = "appointment_notifications"

    __table_args__ = (
        UniqueConstraint(
            "appointment_id",
            "notification_type",
            name="uq_appointment_notification_type",
        ),
        Index(
            "ix_appointment_notifications_status_scheduled",
            "status",
            "scheduled_at",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    appointment_id = Column(
        Integer,
        ForeignKey("appointments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    notification_type = Column(String(30), nullable=False)
    # confirmation or reminder_24h

    status = Column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    # pending, sending, sent, failed, cancelled

    scheduled_at = Column(DateTime(timezone=True), nullable=False)

    sent_at = Column(DateTime(timezone=True), nullable=True)

    attempts = Column(Integer, nullable=False, default=0)

    last_error = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
