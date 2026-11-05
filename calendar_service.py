import logging
import os
import hashlib
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from models import GoogleCalendarConnection
from schemas.appointment import calendar_gmt_datetime, normalize_appointment_utc

logger = logging.getLogger(__name__)


def calendar_event_id_for_booking(business_id: str, idempotency_key: str) -> str:
    """Return a deterministic Google-compatible event ID for a booking key."""
    digest = hashlib.sha256(
        f"{business_id}:{idempotency_key}".encode("utf-8")
    ).hexdigest()
    # Google accepts lowercase base32hex characters; hexadecimal is a subset.
    return f"b{digest}"


def _mark_connection_disconnected(
    db: Session,
    connection: GoogleCalendarConnection,
) -> None:
    """Persist that this connection needs OAuth reconnection, without logging secrets."""
    connection.connected = False
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("Could not persist the Google Calendar reconnection state.")


def _execute_calendar_request(db, connection, request):
    """Execute a Google request and translate revoked credentials safely."""
    try:
        return request.execute()
    except RefreshError:
        _mark_connection_disconnected(db, connection)
        raise HTTPException(
            status_code=503,
            detail=(
                "Google Calendar authorization has expired or was revoked. "
                "Reconnect the calendar before checking availability."
            ),
        ) from None
    except GoogleAuthError:
        raise HTTPException(
            status_code=503,
            detail="Google Calendar authorization could not be validated. Retry or reconnect the calendar.",
        ) from None


def get_calendar_event_for_booking(
    db: Session,
    business_id: str,
    idempotency_key: str,
):
    """Fetch a previously inserted deterministic event, or return None on 404."""
    service = get_calendar_service(db, business_id)
    connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id == business_id,
            GoogleCalendarConnection.connected.is_(True),
        )
        .first()
    )
    if connection is None:
        raise HTTPException(
            status_code=503,
            detail="Google Calendar is not connected for this business.",
        )

    event_id = calendar_event_id_for_booking(business_id, idempotency_key)
    try:
        result = _execute_calendar_request(
            db,
            connection,
            service.events().get(
                calendarId=connection.calendar_id or "primary",
                eventId=event_id,
            ),
        )
    except HttpError as exc:
        if getattr(exc.resp, "status", None) == 404:
            return None
        raise HTTPException(
            status_code=502,
            detail="Google Calendar could not verify the booking event. Retry with the same idempotency key.",
        ) from exc

    stored_key = (
        result.get("extendedProperties", {})
        .get("private", {})
        .get("bookingIdempotencyKey")
    )
    if stored_key != idempotency_key:
        raise HTTPException(
            status_code=409,
            detail="The deterministic Calendar event ID is associated with another booking.",
        )
    if result.get("status") == "cancelled":
        raise HTTPException(
            status_code=409,
            detail="The Calendar event for this booking was cancelled and requires staff review.",
        )

    return {
        "event_id": result["id"],
        "summary": result.get("summary"),
        "start": result.get("start"),
        "end": result.get("end"),
        "html_link": result.get("htmlLink"),
    }
def create_calendar_event(
    db,
    summary: str,
    start_at: datetime,
    end_at: datetime,
    description: str = "",
    business_id: str | None = None,
    idempotency_key: str | None = None,
):


    if (
        start_at.tzinfo is None
        or start_at.utcoffset() is None
        or end_at.tzinfo is None
        or end_at.utcoffset() is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Appointment times must include a timezone.",
        )

    if end_at <= start_at:
        raise HTTPException(
            status_code=400,
            detail="Appointment end must be after its start.",
        )

    if business_id is None:
        business_id = os.getenv(
            "CALENDAR_DEMO_BUSINESS_ID",
            "demo-business",
        )

    service = get_calendar_service(db, business_id)

    connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id == business_id,
            GoogleCalendarConnection.connected.is_(True),
        )
        .first()
    )

    if connection is None:
        raise HTTPException(
            status_code=503,
            detail="Google Calendar is not connected. Reconnect it before using calendar services.",
        )

    start_at = normalize_appointment_utc(start_at)
    end_at = normalize_appointment_utc(end_at)

    event = {
        "summary": summary,
        "description": description,
        "start": calendar_gmt_datetime(start_at),
        "end": calendar_gmt_datetime(end_at),
    }
    if idempotency_key is not None:
        event_id = calendar_event_id_for_booking(business_id, idempotency_key)
        event["id"] = event_id
        event["extendedProperties"] = {
            "private": {"bookingIdempotencyKey": idempotency_key}
        }

        # The event may have been accepted before the previous request or
        # process failed. Look it up before attempting another insert.
        existing = get_calendar_event_for_booking(
            db, business_id, idempotency_key
        )
        if existing is not None:
            return existing

    try:
        result = _execute_calendar_request(
            db,
            connection,
            service.events()
            .insert(
                calendarId=connection.calendar_id or "primary",
                body=event,
            )
        )

        return {
            "event_id": result["id"],
            "summary": result.get("summary"),
            "start": result.get("start"),
            "end": result.get("end"),
            "html_link": result.get("htmlLink"),
        }

    except HttpError as exc:
        if idempotency_key is not None and getattr(exc.resp, "status", None) == 409:
            existing = get_calendar_event_for_booking(
                db, business_id, idempotency_key
            )
            if existing is not None:
                return existing
        raise HTTPException(
            status_code=502,
            detail="Google Calendar could not create the event. Please retry after checking the connection.",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        if idempotency_key is not None:
            # Covers a network timeout after Google accepted the deterministic
            # event ID. A later retry uses the same ID and cannot insert a
            # second event.
            try:
                existing = get_calendar_event_for_booking(
                    db, business_id, idempotency_key
                )
                if existing is not None:
                    return existing
            except Exception:
                logger.warning(
                    "Calendar insert outcome remains uncertain for booking event %s.",
                    calendar_event_id_for_booking(business_id, idempotency_key),
                )
            raise HTTPException(
                status_code=503,
                detail="Google Calendar's booking result is uncertain. Retry using the same idempotency key.",
            ) from exc
        raise


def get_calendar_service(
    db: Session,
    business_id: str | None = None,
):
    if business_id is None:
        business_id = os.getenv(
            "CALENDAR_DEMO_BUSINESS_ID", "demo-business"
        )

    connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id == business_id,
            GoogleCalendarConnection.connected.is_(True),
        )
        .first()
    )

    if not connection:
        raise HTTPException(
            status_code=503,
            detail="Google Calendar is not connected. Reconnect it before using calendar services.",
        )

    encryption_key = os.getenv("CALENDAR_ENCRYPTION_KEY")
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if not all([encryption_key, client_id, client_secret]):
        raise HTTPException(
            status_code=503,
            detail="Google Calendar is unavailable because its server configuration is incomplete.",
        )

    try:
        refresh_token = Fernet(
            encryption_key.encode()
        ).decrypt(
            connection.encrypted_refresh_token.encode()
        ).decode()
    except (InvalidToken, ValueError, AttributeError):
        raise HTTPException(
            status_code=503,
            detail="Google Calendar authorization is unavailable. Reconnect the calendar.",
        )

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/calendar.events"],
    )

    try:
        return build(
            "calendar",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )
    except GoogleAuthError:
        raise HTTPException(
            status_code=503,
            detail="Google Calendar authorization is unavailable. Check the server configuration or reconnect.",
        )


def list_upcoming_events(db: Session, max_results: int = 10):
    service = get_calendar_service(db)

    business_id = os.getenv(
        "CALENDAR_DEMO_BUSINESS_ID", "demo-business"
    )
    connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id == business_id,
            GoogleCalendarConnection.connected.is_(True),
        )
        .first()
    )

    try:
        response = _execute_calendar_request(
            db,
            connection,
            service.events()
            .list(
                calendarId=connection.calendar_id or "primary",
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
                timeMin=datetime.now(timezone.utc).isoformat(),
            )
        )
    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        if status in (401, 403):
            raise HTTPException(
                status_code=502,
                detail=(
                    "Google Calendar access failed. Check permissions "
                    "or reconnect the Google account."
                ),
            )
        raise HTTPException(
            status_code=502,
            detail="Google Calendar could not retrieve events.",
        )

    return [
        {
            "id": event.get("id"),
            "summary": event.get("summary", "(No title)"),
            "start": event.get("start", {}),
            "end": event.get("end", {}),
            "status": event.get("status"),
            "html_link": event.get("htmlLink"),
        }
        for event in response.get("items", [])
    ]


def check_calendar_conflicts(
    db: Session,
    business_id: str,
    start_at: datetime,
    end_at: datetime,
):
    """Return Google Calendar events overlapping the requested interval."""

    if (
        start_at.tzinfo is None
        or start_at.utcoffset() is None
        or end_at.tzinfo is None
        or end_at.utcoffset() is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Appointment times must include a timezone.",
        )

    if end_at <= start_at:
        raise HTTPException(
            status_code=400,
            detail="Appointment end must be after its start.",
        )

    start_utc = start_at.astimezone(timezone.utc)
    end_utc = end_at.astimezone(timezone.utc)

    connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id == business_id,
            GoogleCalendarConnection.connected.is_(True),
        )
        .first()
    )

    if connection is None:
        raise HTTPException(
            status_code=503,
            detail=f"Google Calendar is not connected for business {business_id}.",
        )

    service = get_calendar_service(db, business_id)

    try:
        response = _execute_calendar_request(
            db,
            connection,
            service.events()
            .list(
                calendarId=connection.calendar_id or "primary",
                timeMin=start_utc.isoformat(),
                timeMax=end_utc.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                showDeleted=False,
            )
        )
    except HttpError as exc:
        raise HTTPException(
            status_code=502,
            detail="Google Calendar could not verify availability. Please retry after checking the connection.",
        ) from exc

    conflicts = []

    for event in response.get("items", []):
        if event.get("status") == "cancelled":
            continue

        conflicts.append({
            "event_id": event.get("id"),
            "summary": event.get("summary", "(No title)"),
            "start": event.get("start", {}),
            "end": event.get("end", {}),
        })

    return conflicts

def delete_calendar_event(
    db: Session,
    business_id: str,
    event_id: str,
) -> None:
    """Best-effort cleanup if saving the database booking fails."""
    service = get_calendar_service(db, business_id)

    connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id == business_id,
            GoogleCalendarConnection.connected.is_(True),
        )
        .first()
    )

    if connection is None:
        return

    try:
        _execute_calendar_request(
            db,
            connection,
            service.events()
            .delete(
                calendarId=connection.calendar_id or "primary",
                eventId=event_id,
            )
        )
    except HttpError:
        logger.exception(
            "Could not remove Google Calendar event %s",
            event_id,
        )
def list_calendar_events_in_range(
    db: Session,
    business_id: str,
    start_at: datetime,
    end_at: datetime,
) -> list[dict]:
    """Fetch Google Calendar events for one business across a time range."""
    start_utc = normalize_appointment_utc(start_at)
    end_utc = normalize_appointment_utc(end_at)

    if end_utc <= start_utc:
        raise ValueError("end_at must be later than start_at.")

    connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id == business_id,
            GoogleCalendarConnection.connected.is_(True),
        )
        .first()
    )

    if not connection:
        raise HTTPException(
            status_code=503,
            detail="No connected Google Calendar was found for this business.",
        )

    service = get_calendar_service(db, business_id)
    events = []
    page_token = None

    while True:
        request = service.events().list(
            calendarId=connection.calendar_id,
            timeMin=start_utc.isoformat(),
            timeMax=end_utc.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            showDeleted=False,
            maxResults=2500,
            pageToken=page_token,
        )

        response = _execute_calendar_request(db, connection, request)
        events.extend(
            event for event in response.get("items", [])
            if event.get("status") != "cancelled"
        )

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return events
