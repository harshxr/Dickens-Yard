
import os

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from models import GoogleCalendarConnection

from datetime import datetime
from fastapi import HTTPException
from googleapiclient.errors import HttpError


def create_calendar_event(
    db,
    summary: str,
    start_at: datetime,
    end_at: datetime,
    description: str = "",
):
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise HTTPException(
            status_code=400,
            detail="Appointment times must include a timezone.",
        )

    if end_at <= start_at:
        raise HTTPException(
            status_code=400,
            detail="Appointment end must be after its start.",
        )

    service = get_calendar_service(db)

    connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id
            == os.getenv("CALENDAR_DEMO_BUSINESS_ID", "demo-business"),
            GoogleCalendarConnection.connected.is_(True),
        )
        .first()
    )

    try:
        event = {
            "summary": summary,
            "description": description,
            "start": {
                "dateTime": start_at.isoformat(),
            },
            "end": {
                "dateTime": end_at.isoformat(),
            },
        }

        result = service.events().insert(
            calendarId=connection.calendar_id or "primary",
            body=event,
        ).execute()

        return {
            "event_id": result["id"],
            "summary": result.get("summary"),
            "start": result.get("start"),
            "end": result.get("end"),
            "html_link": result.get("htmlLink"),
        }

    except HttpError as exc:
        raise HTTPException(
            status_code=502,
            detail="Google Calendar could not create the event.",
        ) from exc


def get_calendar_service(db: Session):
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
            status_code=404,
            detail="Google Calendar is not connected.",
        )

    encryption_key = os.getenv("CALENDAR_ENCRYPTION_KEY")
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if not all([encryption_key, client_id, client_secret]):
        raise HTTPException(
            status_code=500,
            detail="Google Calendar configuration is incomplete.",
        )

    try:
        refresh_token = Fernet(
            encryption_key.encode()
        ).decrypt(
            connection.encrypted_refresh_token.encode()
        ).decode()
    except (InvalidToken, ValueError, AttributeError):
        raise HTTPException(
            status_code=500,
            detail="Could not decrypt the saved Google refresh token.",
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
            status_code=502,
            detail="Could not initialize Google Calendar access.",
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
        response = (
            service.events()
            .list(
                calendarId=connection.calendar_id or "primary",
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
                timeMin=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
            )
            .execute()
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
