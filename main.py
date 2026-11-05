
# pyright: reportMissingImports=false

import logging
import os
import re
import uuid
import hashlib
import hmac
import json
import phonenumbers

from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler

from livekit import api
from livekit.api import WebhookReceiver, TokenVerifier
from livekit.api import agent_dispatch_service
from pydantic import BaseModel, Field
from datetime import timezone, datetime, timedelta, time
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, status, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from calendar_oauth import router as google_calendar_router
from calendar_service import (
    check_calendar_conflicts,
    create_calendar_event,
    get_calendar_event_for_booking,
    calendar_event_id_for_booking,
    list_calendar_events_in_range,
)

from database import Base, engine, get_db
from models import (
    Lead,
    Appointment,
    AppointmentNotification,
    CallbackTask,
    GoogleCalendarConnection,
    OutboundCall,
    ProcessedWebhookEvent,
)

from schemas.lead import LeadCreate, LeadResponse, LeadUpdate
from schemas.appointment import (
    AppointmentCreate,
    AppointmentResponse,
    is_within_practice_hours_gmt,
    normalize_appointment_utc,
)
from schemas.callback import (
    CallbackCreate,
    CallbackResponse,
    CallbackStatusUpdate,
)

logger = logging.getLogger(__name__)


class OutboundCallRequest(BaseModel):
    phone: str = Field(min_length=6, max_length=20)
    country: str = Field(min_length=2, max_length=10)
    business_id: str = Field(min_length=1, max_length=100)
    lead_id: int = Field(gt=0)
    purpose: str = Field(min_length=3, max_length=200)
    caller_id: str = Field(min_length=8, max_length=16)
    consent: bool = False
    consent_attestation_id: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=100)


def require_outbound_auth(authorization: str | None = Header(default=None)) -> str:
    """Authenticate trusted server-to-server requests with a bearer secret."""
    configured_token = os.getenv("OUTBOUND_CALL_API_TOKEN", "")
    if len(configured_token) < 32:
        raise HTTPException(
            status_code=503,
            detail="Outbound calling is not configured.",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied_token = authorization[7:]
    if not hmac.compare_digest(supplied_token, configured_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return os.getenv("OUTBOUND_CALL_API_CLIENT", "trusted-outbound-service")


def _is_e164(value: str) -> bool:
    if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
        return False
    try:
        parsed = phonenumbers.parse(value, None)
        return (
            phonenumbers.is_valid_number(parsed)
            and phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            ) == value
        )
    except phonenumbers.NumberParseException:
        return False


def _configured_csv(name: str) -> set[str]:
    return {item.strip() for item in os.getenv(name, "").split(",") if item.strip()}


def _outbound_payload_hash(payload: OutboundCallRequest, e164_phone: str) -> str:
    canonical = {
        "phone": e164_phone,
        "business_id": payload.business_id,
        "lead_id": payload.lead_id,
        "purpose": payload.purpose.strip(),
        "caller_id": payload.caller_id,
        "source": payload.source.strip(),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _acquire_outbound_business_lock(db: Session, business_id: str) -> None:
    """Serialize idempotency and per-business limit checks on PostgreSQL."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        raise HTTPException(
            status_code=503,
            detail="Outbound calling requires PostgreSQL safeguards.",
        )
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_scope))"),
        {"lock_scope": f"outbound-call:{business_id}"},
    )


def _check_outbound_limits(db: Session, business_id: str) -> None:
    try:
        hourly_limit = int(os.getenv("OUTBOUND_MAX_CALLS_PER_BUSINESS_PER_HOUR", "5"))
        concurrent_limit = int(
            os.getenv("OUTBOUND_MAX_CONCURRENT_CALLS_PER_BUSINESS", "1")
        )
    except ValueError:
        raise HTTPException(status_code=503, detail="Outbound calling is not configured.") from None
    if not (1 <= hourly_limit <= 100 and 1 <= concurrent_limit <= 10):
        raise HTTPException(status_code=503, detail="Outbound calling is not configured.")

    hour_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    recent_count = (
        db.query(OutboundCall)
        .filter(
            OutboundCall.business_id == business_id,
            OutboundCall.created_at >= hour_ago,
        )
        .count()
    )
    active_count = (
        db.query(OutboundCall)
        .filter(
            OutboundCall.business_id == business_id,
            OutboundCall.status.in_(("initiated", "ringing", "active")),
        )
        .count()
    )
    if recent_count >= hourly_limit or active_count >= concurrent_limit:
        raise HTTPException(status_code=429, detail="Outbound call limit reached.")


def normalize_phone_number(country: str, raw: str) -> str:
    country = country.strip()
    raw = raw.strip()

    if not re.fullmatch(r"\+\d{1,3}", country):
        raise HTTPException(status_code=400, detail="Invalid country calling code.")

    try:
        calling_code = int(country[1:])

        if raw.startswith("+"):
            parsed = phonenumbers.parse(raw, None)
        else:
            region = phonenumbers.region_code_for_country_code(calling_code)
            if not region or region == "ZZ":
                raise ValueError("Unsupported country calling code.")
            parsed = phonenumbers.parse(raw, region)

        if parsed.country_code != calling_code:
            raise ValueError("Phone number does not match the selected country.")

        if not phonenumbers.is_valid_number(parsed):
            raise ValueError("Please enter a valid phone number.")

        return phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.E164
        )

    except (phonenumbers.NumberParseException, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def normalize_appointment_request_time(value: datetime, field_name: str) -> datetime:
    """Require an offset at HTTP boundaries and return a UTC-aware instant."""
    try:
        return normalize_appointment_utc(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{field_name} must include a timezone offset. "
                "Send an ISO 8601 timestamp ending in Z or an explicit ±HH:MM offset."
            ),
        ) from None


def appointment_request_fingerprint(
    appointment: AppointmentCreate,
    start_at: datetime,
    end_at: datetime,
) -> str:
    """Hash normalized booking details for same-key payload validation."""
    normalized = {
        "business_id": appointment.business_id,
        "lead_id": appointment.lead_id,
        "service": appointment.service,
        "start_at": normalize_appointment_utc(start_at).isoformat(),
        "end_at": normalize_appointment_utc(end_at).isoformat(),
        "notes": appointment.notes,
    }
    serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def mark_booking_reconciliation_required(
    db: Session,
    business_id: str,
    idempotency_key: str,
    appointment_id: int,
) -> None:
    """Keep the slot reserved and persist that Calendar completion needs retry."""
    db.rollback()
    try:
        booking = (
            db.query(Appointment)
            .filter(
                Appointment.business_id == business_id,
                Appointment.idempotency_key == idempotency_key,
            )
            .with_for_update()
            .one()
        )
        if booking.booking_state != "confirmed":
            booking.status = "pending"
            booking.booking_state = "reconciliation_required"
            db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception(
            "Could not persist recovery state for appointment %s. Error: %s",
            appointment_id,
            type(exc).__name__,
        )


def appointment_booking_result(booking: Appointment) -> dict:
    return {
        "success": True,
        "message": "Appointment booked successfully.",
        "appointment_id": booking.id,
        "business_id": booking.business_id,
        "lead_id": booking.lead_id,
        "service": booking.service,
        "start_at": normalize_appointment_utc(booking.start_at).isoformat(),
        "end_at": normalize_appointment_utc(booking.end_at).isoformat(),
        "timezone": "GMT",
        "status": booking.status,
        "calendar_event_id": booking.calendar_event_id,
        "calendar_link": booking.calendar_event_link,
        "idempotency_key": booking.idempotency_key,
    }

# Schema creation is an explicit local-development action. Production schema
# changes are applied from reviewed migrations, not as an application side effect.
if os.getenv("AUTO_CREATE_TABLES", "false").strip().lower() == "true":
    logger.warning("AUTO_CREATE_TABLES is enabled; creating missing database tables.")
    Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = None

    email_worker_enabled = (
        os.getenv("EMAIL_WORKER_ENABLED", "false").strip().lower() == "true"
    )

    if email_worker_enabled:
        from email_worker import process_appointment_notifications

        scheduler = BackgroundScheduler(timezone="UTC")

        scheduler.add_job(
            process_appointment_notifications,
            trigger="interval",
            seconds=30,
            id="appointment_email_worker",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )

        scheduler.start()
        logger.info("Appointment email worker started.")

    else:
        logger.info("Appointment email worker is disabled.")

    try:
        yield
    finally:
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("Appointment email worker stopped.")


app = FastAPI(
    title="Voice Agent Platform API",
    description="Backend for managing AI voice agents, leads, appointments, and callbacks",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
    "https://nivakya.pro",
    "https://www.nivakya.pro",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Idempotency-Key"],
)

app.include_router(google_calendar_router)

# --------------------------------------------------
# LIVEKIT WEBHOOK
# --------------------------------------------------

@app.post("/api/v1/webhooks/livekit")
async def livekit_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    # Read the original request body. LiveKit signature verification
    # requires the exact raw body.
    raw_body = await request.body()

    authorization = request.headers.get("authorization", "")
    scheme, separator, auth_token = authorization.partition(" ")

    if not separator or scheme.lower() != "bearer" or not auth_token.strip():
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid webhook authorization.",
        )

    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not api_key or not api_secret:
        logger.error("LiveKit webhook credentials are not configured.")
        raise HTTPException(
            status_code=503,
            detail="Webhook verification is not configured.",
        )

    # Verify the LiveKit signature before trusting the event.
    try:
        receiver = WebhookReceiver(TokenVerifier(api_key, api_secret))
        event = receiver.receive(
            raw_body.decode("utf-8"),
            auth_token.strip(),
        )
    except Exception:
        logger.warning("Rejected LiveKit webhook: signature or payload verification failed.")
        raise HTTPException(
            status_code=401,
            detail="Webhook verification failed.",
        ) from None

    event_id = event.id
    event_type = event.event
    room_name = event.room.name if event.HasField("room") else None

    if not event_id or not event_type:
        raise HTTPException(
            status_code=400,
            detail="Webhook event ID or event type is missing.",
        )

    if len(event_id) > 255 or len(event_type) > 100:
        raise HTTPException(
            status_code=400,
            detail="Webhook event fields exceed the allowed length.",
        )

    if room_name and len(room_name) > 150:
        raise HTTPException(
            status_code=400,
            detail="Webhook room name is too long.",
        )

    # Prepare the event record. The unique event_id prevents reprocessing.
    webhook_record = ProcessedWebhookEvent(
        event_id=event_id,
        event_type=event_type,
        room_name=room_name,
    )

    try:
        db.add(webhook_record)

        # Only update a call created by our outbound-call endpoint.
        call_record = None
        if room_name:
            call_record = (
                db.query(OutboundCall)
                .filter(OutboundCall.room_name == room_name)
                .first()
            )

        new_status = None

        # Only interpret lifecycle events for SIP participants.
        if call_record and event.HasField("participant"):
            participant = event.participant

            # LiveKit ParticipantInfo kind 3 represents a SIP participant.
            if participant.kind == 3:
                sip_status = participant.attributes.get("sip.callStatus", "")

                if sip_status == "ringing":
                    new_status = "ringing"
                elif sip_status == "active":
                    new_status = "in_progress"
                elif sip_status == "hangup":
                    new_status = "completed"
                elif event_type == "participant_connection_aborted":
                    new_status = "failed"
                elif event_type == "participant_left":
                    new_status = "completed"

        # Never overwrite a terminal call status.
        terminal_statuses = {"completed", "failed", "cancelled"}

        if (
            call_record
            and new_status
            and call_record.status not in terminal_statuses
        ):
            call_record.status = new_status

        db.commit()

    except IntegrityError:
        db.rollback()
        return {
            "success": True,
            "duplicate": True,
            "message": "Webhook event was already processed.",
        }

    except Exception:
        db.rollback()
        logger.exception("Could not process verified LiveKit webhook.")
        raise HTTPException(
            status_code=500,
            detail="Could not process webhook event.",
        ) from None

    return {
        "success": True,
        "duplicate": False,
        "event": event_type,
        "call_updated": bool(call_record and new_status),
    }

# --------------------------------------------------
# HOME AND HEALTH CHECK
# --------------------------------------------------

@app.get("/")
def home():
    return {"message": "Voice Agent Platform API is running"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/ready")
def readiness_check():
    """Readiness probe; performs only a read-only PostgreSQL connectivity query."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable.",
        ) from None
    return {"status": "ready"}


@app.get("/api/v1")
def api_info():
    return {
        "name": "Voice Agent Platform",
        "version": "1.0.0",
        "features": [
            "Lead management",
            "Voice agent integration",
            "Call management",
            "Appointment booking",
            "Callback management",
        ],
    }
# --------------------------------------------------
# OUTBOUND CALL REQUEST
# --------------------------------------------------

@app.post("/api/v1/outbound-calls", status_code=status.HTTP_202_ACCEPTED)
async def request_outbound_call(
    payload: OutboundCallRequest,
    db: Session = Depends(get_db),
    client_name: str = Depends(require_outbound_auth),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if payload.consent is not True or not payload.consent_attestation_id.strip():
        raise HTTPException(
            status_code=400,
            detail="A verified consent attestation is required.",
        )

    if not idempotency_key or not re.fullmatch(r"[A-Za-z0-9._:-]{16,128}", idempotency_key):
        raise HTTPException(status_code=400, detail="A valid Idempotency-Key header is required.")

    if not payload.purpose.strip() or not payload.source.strip():
        raise HTTPException(status_code=400, detail="Call purpose and consent source are required.")

    business_id = os.getenv("OUTBOUND_CALL_BUSINESS_ID", "").strip()
    allowed_caller_ids = _configured_csv("OUTBOUND_ALLOWED_CALLER_IDS")
    if not business_id or not allowed_caller_ids:
        raise HTTPException(status_code=503, detail="Outbound calling is not configured.")
    if payload.business_id != business_id:
        raise HTTPException(status_code=403, detail="Business is not authorized for outbound calls.")

    # Both destination and caller ID must already be valid E.164 numbers.
    e164_phone = normalize_phone_number(payload.country, payload.phone)
    if payload.phone != e164_phone:
        raise HTTPException(status_code=400, detail="Destination phone must be in E.164 format.")
    if not _is_e164(payload.caller_id) or payload.caller_id not in allowed_caller_ids:
        raise HTTPException(status_code=403, detail="Caller ID is not authorized.")

    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", payload.consent_attestation_id):
        raise HTTPException(status_code=400, detail="Consent attestation reference is invalid.")

    lead = db.get(Lead, payload.lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead was not found.")

    calls_enabled = os.getenv("OUTBOUND_CALLS_ENABLED", "false").strip().lower() == "true"
    test_mode = os.getenv("OUTBOUND_CALL_TEST_MODE", "false").strip().lower() == "true"
    if test_mode and calls_enabled:
        raise HTTPException(status_code=503, detail="Outbound test mode conflicts with the global disable.")
    if not calls_enabled and not test_mode:
        raise HTTPException(
            status_code=503,
            detail="Outbound calling is currently disabled.",
        )
    if test_mode:
        test_numbers = _configured_csv("OUTBOUND_TEST_ALLOWED_NUMBERS")
        if not test_numbers or e164_phone not in test_numbers:
            raise HTTPException(status_code=403, detail="Destination is not an approved test number.")
        # Validation-only mode intentionally makes no database writes or LiveKit calls.
        return {
            "success": True,
            "test_mode": True,
            "message": "Request validated. No call was created.",
        }

    livekit_url = os.getenv("LIVEKIT_URL")
    livekit_api_key = os.getenv("LIVEKIT_API_KEY")
    livekit_api_secret = os.getenv("LIVEKIT_API_SECRET")
    trunk_id = os.getenv("LIVEKIT_OUTBOUND_TRUNK_ID")
    agent_name = os.getenv("LIVEKIT_AGENT_NAME", "Dental-Ealing")

    if not all([
        livekit_url,
        livekit_api_key,
        livekit_api_secret,
        trunk_id,
        agent_name,
    ]):
        raise HTTPException(
            status_code=503,
            detail="Outbound calling configuration is incomplete.",
        )

    import hashlib
    from google.protobuf.duration_pb2 import Duration

    phone_hash = hashlib.sha256(e164_phone.encode("utf-8")).hexdigest()
    request_hash = _outbound_payload_hash(payload, e164_phone)
    idempotency_hash = hashlib.sha256(
        f"{business_id}:{idempotency_key}".encode("utf-8")
    ).hexdigest()
    masked_phone = (
        e164_phone[:4] + "*" * max(0, len(e164_phone) - 6) + e164_phone[-2:]
    )
    room_name = f"outbound-{uuid.uuid4().hex[:12]}"

    try:
        _acquire_outbound_business_lock(db, business_id)
        prior = (
            db.query(OutboundCall)
            .filter(
                OutboundCall.business_id == business_id,
                OutboundCall.idempotency_key_hash == idempotency_hash,
            )
            .first()
        )
        if prior is not None:
            db.rollback()
            if prior.request_hash != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was already used for different call details.",
                )
            raise HTTPException(
                status_code=409,
                detail="This call request has already been processed.",
            )

        duplicate_payload = (
            db.query(OutboundCall)
            .filter(
                OutboundCall.business_id == business_id,
                OutboundCall.request_hash == request_hash,
                OutboundCall.created_at
                >= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10),
            )
            .first()
        )
        if duplicate_payload is not None:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="An equivalent call request was recently submitted.",
            )
        _check_outbound_limits(db, business_id)

        call_record = OutboundCall(
            business_id=business_id,
            lead_id=lead.id,
            idempotency_key_hash=idempotency_hash,
            request_hash=request_hash,
            purpose=payload.purpose.strip(),
            caller_id=payload.caller_id,
            consent_attestation_id=payload.consent_attestation_id,
            requested_by=client_name,
            room_name=room_name,
            phone_masked=masked_phone,
            phone_hash=phone_hash,
            status="initiated",
            consent=True,
            # This is the authenticated attestation receipt time, not a claim
            # that the request timestamp independently proves legal consent.
            consent_at=datetime.now(timezone.utc),
            source=payload.source.strip(),
        )
        db.add(call_record)
        db.commit()
        db.refresh(call_record)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        logger.info("Duplicate outbound request rejected (error type: %s)", type(exc).__name__)
        raise HTTPException(status_code=409, detail="Duplicate call request.") from None
    except Exception as exc:
        db.rollback()
        logger.error(
            "Could not save outbound call record (error type: %s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail="Could not create the call request.",
        ) from exc

    lk = None

    try:
        lk = api.LiveKitAPI(
            url=livekit_url,
            api_key=livekit_api_key,
            api_secret=livekit_api_secret,
        )

        # Dispatch the configured voice agent into the new room.
        dispatch_request = agent_dispatch_service.CreateAgentDispatchRequest(
            agent_name=agent_name,
            room=room_name,
        )

        await lk.agent_dispatch.create_dispatch(dispatch_request)

        # Connect the destination phone number through the outbound SIP trunk.
        sip_request = api.CreateSIPParticipantRequest(
            sip_call_to=e164_phone,
            sip_number=payload.caller_id,
            room_name=room_name,
            participant_identity=f"phone-{uuid.uuid4().hex[:12]}",
            participant_name="Website Caller",
            wait_until_answered=False,
            max_call_duration=Duration(seconds=300),
        )

        sip_info = await lk.sip.create_sip_participant(
            sip_request,
            trunk_id=trunk_id,
        )

        call_record.sip_call_id = sip_info.sip_call_id
        call_record.status = "initiated"
        db.commit()

        return {
            "success": True,
            "message": "Your call request has been submitted.",
            "call_id": call_record.id,
            "status": call_record.status,
            "phone": masked_phone,
            "business_id": business_id,
            "lead_id": lead.id,
        }

    except Exception as exc:
        db.rollback()

        call_record.status = "failed"
        db.add(call_record)
        db.commit()

        logger.error(
            "Outbound call failed (error type: %s)",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=502,
            detail="The call could not be started. Please try again later.",
        ) from exc

    finally:
        if lk is not None:
            await lk.aclose()

# --------------------------------------------------
# LEAD MANAGEMENT
# --------------------------------------------------

@app.post(
    "/api/v1/leads",
    response_model=LeadResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_lead(
    lead: LeadCreate,
    db: Session = Depends(get_db),
):
    new_lead = Lead(**lead.model_dump())

    try:
        db.add(new_lead)
        db.commit()
        db.refresh(new_lead)
        return new_lead
    except Exception:
        db.rollback()
        raise


@app.get(
    "/api/v1/leads",
    response_model=list[LeadResponse],
)
def get_all_leads(db: Session = Depends(get_db)):
    return db.query(Lead).order_by(Lead.id).all()


@app.get(
    "/api/v1/leads/{lead_id}",
    response_model=LeadResponse,
)
def get_lead(
    lead_id: int,
    db: Session = Depends(get_db),
):
    lead = db.get(Lead, lead_id)

    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )

    return lead


@app.put(
    "/api/v1/leads/{lead_id}",
    response_model=LeadResponse,
)
def update_lead(
    lead_id: int,
    updates: LeadUpdate,
    db: Session = Depends(get_db),
):
    lead = db.get(Lead, lead_id)

    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )

    changes = updates.model_dump(exclude_unset=True)

    for key, value in changes.items():
        setattr(lead, key, value)

    try:
        db.commit()
        db.refresh(lead)
        return lead
    except Exception:
        db.rollback()
        raise


@app.delete("/api/v1/leads/{lead_id}")
def delete_lead(
    lead_id: int,
    db: Session = Depends(get_db),
):
    lead = db.get(Lead, lead_id)

    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )

    try:
        db.delete(lead)
        db.commit()
        return {
            "message": f"Lead {lead_id} deleted successfully"
        }
    except Exception:
        db.rollback()
        raise


# --------------------------------------------------
# APPOINTMENT MANAGEMENT
# --------------------------------------------------

@app.get("/api/v1/appointments/availability")
def check_appointment_availability(
    business_id: str = Query(..., min_length=1, max_length=100),
    start_at: datetime = Query(...),
    end_at: datetime = Query(...),
    db: Session = Depends(get_db),
):
    start_utc = normalize_appointment_request_time(start_at, "start_at")
    end_utc = normalize_appointment_request_time(end_at, "end_at")

    if end_utc <= start_utc:
        raise HTTPException(
            status_code=400,
            detail="end_at must be later than start_at.",
        )

    if not is_within_practice_hours_gmt(start_utc, end_utc):
        return {
            "business_id": business_id,
            "available": False,
            "message": "Appointments are offered Monday to Saturday, 09:00–17:00 fixed GMT (UTC+00:00).",
            "timezone": "GMT",
        }

    conflict = (
        db.query(Appointment)
        .filter(
            Appointment.business_id == business_id,
            Appointment.status != "cancelled",
            Appointment.start_at < end_utc,
            Appointment.end_at > start_utc,
        )
        .first()
    )

    if conflict:
        return {
            "business_id": business_id,
            "available": False,
            "message": "The requested time conflicts with an existing appointment.",
        }


    # OAuth management routes are locally gated; appointment availability still
    # requires the stored business Calendar connection. Service failures propagate
    # as errors so this endpoint never reports a free slot without checking Google.
    calendar_conflicts = check_calendar_conflicts(
        db=db,
        business_id=business_id,
        start_at=start_utc,
        end_at=end_utc,
    )

    if calendar_conflicts:
        return {
            "business_id": business_id,
            "available": False,
            "message": "The requested time conflicts with a Google Calendar event.",
        }

    return {
        "business_id": business_id,
        "available": True,
        "message": "No conflicting appointment or Google Calendar event was found.",
    }

@app.get("/api/v1/appointments/alternatives")
def get_appointment_alternatives(
    business_id: str = Query(..., min_length=1, max_length=100),
    start_at: datetime = Query(...),
    end_at: datetime = Query(...),
    max_results: int = Query(3, ge=1, le=3),
    days_ahead: int = Query(7, ge=1, le=14),
    db: Session = Depends(get_db),
):
    """Find verified alternative appointment slots in Europe/London."""
    london_tz = ZoneInfo("Europe/London")

    requested_start = normalize_appointment_request_time(
        start_at, "start_at"
    )
    requested_end = normalize_appointment_request_time(
        end_at, "end_at"
    )

    if requested_end <= requested_start:
        raise HTTPException(
            status_code=400,
            detail="end_at must be later than start_at.",
        )

    duration = requested_end - requested_start
    requested_local = requested_start.astimezone(london_tz)
    now_local = datetime.now(london_tz)

    search_start = now_local.astimezone(timezone.utc)
    search_end_local = (
        requested_local.date() + timedelta(days=days_ahead + 1)
    )
    search_end = datetime.combine(
        search_end_local,
        time.min,
        tzinfo=london_tz,
    ).astimezone(timezone.utc)

    # Fetch database appointments once for the entire search window.
    db_conflicts = (
        db.query(Appointment)
        .filter(
            Appointment.business_id == business_id,
            Appointment.status != "cancelled",
            Appointment.start_at < search_end,
            Appointment.end_at > search_start,
        )
        .all()
    )

    # Fetch Google Calendar events once for the entire search window.
    calendar_events = list_calendar_events_in_range(
        db=db,
        business_id=business_id,
        start_at=search_start,
        end_at=search_end,
    )

    def event_interval(event):
        event_start = event.get("start", {})
        event_end = event.get("end", {})

        if event_start.get("dateTime") and event_end.get("dateTime"):
            start_value = datetime.fromisoformat(
                event_start["dateTime"].replace("Z", "+00:00")
            )
            end_value = datetime.fromisoformat(
                event_end["dateTime"].replace("Z", "+00:00")
            )
            return (
                normalize_appointment_utc(start_value),
                normalize_appointment_utc(end_value),
            )

        # All-day Google Calendar events use exclusive end dates.
        if event_start.get("date") and event_end.get("date"):
            all_day_start = datetime.combine(
                datetime.fromisoformat(event_start["date"]).date(),
                time.min,
                tzinfo=london_tz,
            )
            all_day_end = datetime.combine(
                datetime.fromisoformat(event_end["date"]).date(),
                time.min,
                tzinfo=london_tz,
            )
            return (
                all_day_start.astimezone(timezone.utc),
                all_day_end.astimezone(timezone.utc),
            )

        return None

    calendar_intervals = [
        interval
        for event in calendar_events
        if (interval := event_interval(event)) is not None
    ]

    alternatives = []

    # Search the requested London date first, then later dates.
    for day_offset in range(days_ahead + 1):
        candidate_date = requested_local.date() + timedelta(days=day_offset)

        # Sunday is closed.
        if candidate_date.weekday() >= 6:
            continue

        candidates = []

        for minutes in range(9 * 60, 17 * 60, 30):
            hour, minute = divmod(minutes, 60)
            candidate_local = datetime(
                candidate_date.year,
                candidate_date.month,
                candidate_date.day,
                hour,
                minute,
                tzinfo=london_tz,
            )

            candidate_end_local = candidate_local + duration

            if candidate_end_local.date() != candidate_date:
                continue

            if candidate_end_local.time() > time(17):
                continue

            if candidate_local <= now_local:
                continue

            candidate_start = candidate_local.astimezone(timezone.utc)
            candidate_end = candidate_end_local.astimezone(timezone.utc)

            # Do not offer the exact time originally requested.
            if candidate_start == requested_start:
                continue

            candidates.append(
                (candidate_start, candidate_end, candidate_local)
            )

        # On the requested day, offer the nearest alternatives first.
        if day_offset == 0:
            candidates.sort(
                key=lambda item: abs(
                    (item[0] - requested_start).total_seconds()
                )
            )

        for candidate_start, candidate_end, candidate_local in candidates:
            database_busy = any(
                appointment.start_at < candidate_end
                and appointment.end_at > candidate_start
                for appointment in db_conflicts
            )

            calendar_busy = any(
                event_start < candidate_end
                and event_end > candidate_start
                for event_start, event_end in calendar_intervals
            )

            if database_busy or calendar_busy:
                continue

            alternatives.append({
                "start_at": candidate_local.isoformat(),
                "end_at": (
                    candidate_local + duration
                ).isoformat(),
                "timezone": "Europe/London",
            })

            if len(alternatives) >= max_results:
                break

        if len(alternatives) >= max_results:
            break

    return {
        "business_id": business_id,
        "timezone": "Europe/London",
        "duration_minutes": int(duration.total_seconds() // 60),
        "alternatives": alternatives,
        "count": len(alternatives),
        "message": (
            "Verified alternative slots found."
            if alternatives
            else "No verified alternatives were found in the requested search period."
        ),
    }


@app.post(
    "/api/v1/appointments",
    response_model=AppointmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_appointment(
    appointment: AppointmentCreate,
    db: Session = Depends(get_db),
):
    start_at = normalize_appointment_request_time(appointment.start_at, "start_at")
    end_at = normalize_appointment_request_time(appointment.end_at, "end_at")

    lead = db.get(Lead, appointment.lead_id)

    if lead is None:
        raise HTTPException(
            status_code=404,
            detail="Lead not found",
        )

    if not is_within_practice_hours_gmt(start_at, end_at):
        raise HTTPException(
            status_code=400,
            detail="Appointments must be Monday to Saturday, 09:00–17:00 fixed GMT (UTC+00:00).",
        )

    conflict = (
        db.query(Appointment)
        .filter(
            Appointment.business_id == appointment.business_id,
            Appointment.status != "cancelled",
            Appointment.start_at < end_at,
            Appointment.end_at > start_at,
        )
        .first()
    )

    if conflict:
        raise HTTPException(
            status_code=409,
            detail="This business already has an appointment during the requested time.",
        )

    new_appointment = Appointment(
        **appointment.model_dump(
            exclude={"start_at", "end_at"}
        ),
        start_at=start_at,
        end_at=end_at,
    )

    try:
        db.add(new_appointment)
        db.commit()
        db.refresh(new_appointment)
        return AppointmentResponse(
            id=new_appointment.id,
            business_id=new_appointment.business_id,
            lead_id=new_appointment.lead_id,
            service=new_appointment.service,
            start_at=normalize_appointment_utc(new_appointment.start_at),
            end_at=normalize_appointment_utc(new_appointment.end_at),
            status=new_appointment.status,
            notes=new_appointment.notes,
            created_at=normalize_appointment_utc(new_appointment.created_at),
        )
    except Exception:
        db.rollback()
        raise

def schedule_appointment_notifications(db, booking, lead):
    """
    Add confirmation and 24-hour reminder records to the notification queue.
    The caller commits these records with the confirmed appointment.
    """
    if not lead.email or not lead.email.strip():
        logger.warning(
            "Appointment %s has no patient email. Notifications skipped.",
            booking.id,
        )
        return

    now = datetime.now(timezone.utc)

    db.execute(
        text("""
            INSERT INTO appointment_notifications
                (appointment_id, notification_type, status, scheduled_at)
            VALUES
                (:appointment_id, 'confirmation', 'pending', :scheduled_at)
            ON CONFLICT (appointment_id, notification_type) DO NOTHING
        """),
        {
            "appointment_id": booking.id,
            "scheduled_at": now,
        },
    )

    reminder_at = booking.start_at - timedelta(hours=24)

    if reminder_at > now:
        db.execute(
            text("""
                INSERT INTO appointment_notifications
                    (appointment_id, notification_type, status, scheduled_at)
                VALUES
                    (:appointment_id, 'reminder_24h', 'pending', :scheduled_at)
                ON CONFLICT (appointment_id, notification_type) DO NOTHING
            """),
            {
                "appointment_id": booking.id,
                "scheduled_at": reminder_at,
            },
        )
    else:
        logger.info(
            "Appointment %s is less than 24 hours away; 24-hour reminder skipped.",
            booking.id,
        )
@app.post("/api/v1/appointments/book")
def book_appointment(
    appointment: AppointmentCreate,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    start_utc = normalize_appointment_request_time(appointment.start_at, "start_at")
    end_utc = normalize_appointment_request_time(appointment.end_at, "end_at")

    request_hash = appointment_request_fingerprint(appointment, start_utc, end_utc)
    # The fallback preserves older callers and makes their exact normalized
    # request replay-safe. New clients send their own stable key explicitly.
    if idempotency_key is None:
        idempotency_key = f"legacy-{request_hash}"
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key must contain 1–128 letters, digits, '.', '_', ':', or '-'.",
        )

    # Resolve successful retries before depending on a live lead or Calendar
    # connection. The database result remains replayable after a restart.
    booking = (
        db.query(Appointment)
        .filter(
            Appointment.business_id == appointment.business_id,
            Appointment.idempotency_key == idempotency_key,
        )
        .first()
    )
    if booking is not None and booking.request_hash != request_hash:
        raise HTTPException(
            status_code=409,
            detail="This Idempotency-Key was already used with different booking details.",
        )
    if booking is not None and booking.booking_state == "confirmed":
        if not booking.calendar_event_id:
            raise HTTPException(
                status_code=503,
                detail="The saved booking is missing its Calendar reference and requires staff review.",
            )
        return appointment_booking_result(booking)

    if not is_within_practice_hours_gmt(start_utc, end_utc):
        raise HTTPException(
            status_code=400,
            detail="Appointments must be Monday to Saturday, 09:00–17:00 fixed GMT (UTC+00:00).",
        )

    lead = db.get(Lead, appointment.lead_id)
    if lead is None:
        raise HTTPException(
            status_code=404,
            detail="Lead not found.",
        )

    # Serialize reservations for this business using its existing Calendar
    # connection row. This coordinates different keys competing for one slot.
    calendar_connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id == appointment.business_id,
            GoogleCalendarConnection.connected.is_(True),
        )
        .with_for_update()
        .first()
    )
    if calendar_connection is None:
        raise HTTPException(
            status_code=503,
            detail="Google Calendar is not connected for this business. The appointment was not booked.",
        )

    booking = (
        db.query(Appointment)
        .filter(
            Appointment.business_id == appointment.business_id,
            Appointment.idempotency_key == idempotency_key,
        )
        .with_for_update()
        .first()
    )

    if booking is not None and booking.request_hash != request_hash:
        raise HTTPException(
            status_code=409,
            detail="This Idempotency-Key was already used with different booking details.",
        )

    if booking is None:
        conflict = (
            db.query(Appointment)
            .filter(
                Appointment.business_id == appointment.business_id,
                Appointment.status != "cancelled",
                Appointment.start_at < end_utc,
                Appointment.end_at > start_utc,
            )
            .first()
        )
        if conflict:
            raise HTTPException(
                status_code=409,
                detail="That appointment slot is already booked.",
            )

        conflicts = check_calendar_conflicts(
            db=db,
            business_id=appointment.business_id,
            start_at=start_utc,
            end_at=end_utc,
        )
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail="That appointment slot conflicts with Google Calendar.",
            )

        booking = Appointment(
            business_id=appointment.business_id,
            lead_id=appointment.lead_id,
            service=appointment.service,
            start_at=start_utc,
            end_at=end_utc,
            notes=appointment.notes,
            status="pending",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            booking_state="calendar_pending",
        )
        db.add(booking)
        try:
            # Persist a slot reservation and idempotency record before external
            # I/O. It survives an application restart during Calendar creation.
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error(
                "Could not reserve appointment for business %s (error type %s).",
                appointment.business_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="The appointment could not be reserved. It is not confirmed.",
            ) from exc

    if booking is not None and booking.booking_state == "confirmed":
        if not booking.calendar_event_id:
            raise HTTPException(
                status_code=503,
                detail="The saved booking is missing its Calendar reference and requires staff review.",
            )
        return appointment_booking_result(booking)

    if booking.booking_state == "calendar_conflict":
        booking.status = "pending"
        booking.booking_state = "calendar_pending"
        db.commit()

    # After a new reservation commit, take a row lock before Calendar work.
    # Retries on any worker serialize on this durable booking row.
    try:
        booking = (
            db.query(Appointment)
            .filter(Appointment.id == booking.id)
            .with_for_update()
            .one()
        )
    except Exception as exc:
        mark_booking_reconciliation_required(
            db, appointment.business_id, idempotency_key, booking.id
        )
        raise HTTPException(
            status_code=503,
            detail="The appointment is saved as pending and is not confirmed. Retry the same request with the same Idempotency-Key.",
        ) from exc
    if booking.booking_state == "confirmed":
        return appointment_booking_result(booking)

    try:
        event = get_calendar_event_for_booking(
            db, appointment.business_id, idempotency_key
        )

        if event is None:
            conflicts = check_calendar_conflicts(
                db=db,
                business_id=appointment.business_id,
                start_at=start_utc,
                end_at=end_utc,
            )
            if conflicts:
                booking.status = "cancelled"
                booking.booking_state = "calendar_conflict"
                db.commit()
                raise HTTPException(
                    status_code=409,
                    detail="That appointment slot conflicts with Google Calendar.",
                )

            event = create_calendar_event(
                db=db,
                business_id=appointment.business_id,
                summary=f"Dental appointment - {lead.name}",
                start_at=start_utc,
                end_at=end_utc,
                description=(
                    f"Service: {appointment.service}\n"
                    f"Patient: {lead.name}\n"
                    f"Phone: {lead.phone}\n"
                    f"Lead ID: {lead.id}\n"
                    f"Business ID: {appointment.business_id}"
                ),
                idempotency_key=idempotency_key,
            )

        booking.calendar_event_id = event["event_id"]
        booking.calendar_event_link = event.get("html_link")
        booking.booking_state = "confirmed"
        booking.status = "confirmed"

        schedule_appointment_notifications(db, booking, lead)

        db.commit()

    except HTTPException as exc:
        if exc.status_code == 409 and booking.booking_state == "calendar_conflict":
            raise
        mark_booking_reconciliation_required(
            db, appointment.business_id, idempotency_key, booking.id
        )
        logger.warning(
            "Booking %s requires Calendar reconciliation (event %s).",
            booking.id,
            calendar_event_id_for_booking(appointment.business_id, idempotency_key),
        )
        raise HTTPException(
            status_code=503,
            detail="The appointment is saved as pending and is not confirmed. Retry the same request with the same Idempotency-Key.",
        ) from exc
    except Exception as exc:
        logger.exception(
            "Booking %s requires Calendar reconciliation. Error type: %s. Error: %s",
            booking.id,
            type(exc).__name__,
            str(exc),
        )
        mark_booking_reconciliation_required(
            db, appointment.business_id, idempotency_key, booking.id
        )
        raise HTTPException(
            status_code=503,
            detail="The appointment is saved as pending and is not confirmed. Retry the same request with the same Idempotency-Key.",
        ) from exc

    return appointment_booking_result(booking)

# --------------------------------------------------
# APPOINTMENT RETRIEVAL
# --------------------------------------------------

@app.get(
    "/api/v1/appointments",
    response_model=list[AppointmentResponse],
)
def get_all_appointments(db: Session = Depends(get_db)):
    return db.query(Appointment).order_by(Appointment.id).all()


@app.get(
    "/api/v1/appointments/{appointment_id}",
    response_model=AppointmentResponse,
)
def get_appointment(
    appointment_id: int,
    db: Session = Depends(get_db),
):
    appointment = db.get(Appointment, appointment_id)

    if appointment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Appointment not found",
        )

    return appointment



# --------------------------------------------------
# CALLBACK MANAGEMENT
# --------------------------------------------------

@app.post(
    "/api/v1/callbacks",
    response_model=CallbackResponse,
    status_code=status.HTTP_201_CREATED,
)

def create_callback(
    task: CallbackCreate,
    db: Session = Depends(get_db),
):
    # Verify that the lead exists
    lead = db.get(Lead, task.lead_id)

    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )

    new_callback = CallbackTask(**task.model_dump())

    try:
        db.add(new_callback)
        db.commit()
        db.refresh(new_callback)
        return new_callback
    except Exception:
        db.rollback()
        raise


@app.get(
    "/api/v1/callbacks",
    response_model=list[CallbackResponse],
)
def get_all_callbacks(db: Session = Depends(get_db)):
    return db.query(CallbackTask).order_by(CallbackTask.id).all()


@app.get(
    "/api/v1/callbacks/{callback_id}",
    response_model=CallbackResponse,
)
def get_callback(
    callback_id: int,
    db: Session = Depends(get_db),
):
    callback = db.get(CallbackTask, callback_id)

    if callback is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Callback task not found",
        )

    return callback


@app.patch(
    "/api/v1/callbacks/{callback_id}/status",
    response_model=CallbackResponse,
)
def update_callback_status(
    callback_id: int,
    status_update: CallbackStatusUpdate,
    db: Session = Depends(get_db),
):
    callback = db.get(CallbackTask, callback_id)

    if callback is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Callback task not found",
        )

    changes = status_update.model_dump(exclude_unset=True)

    for key, value in changes.items():
        setattr(callback, key, value)

    try:
        db.commit()
        db.refresh(callback)
        return callback
    except Exception:
        db.rollback()
        raise

