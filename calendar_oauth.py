
import os
import secrets
from urllib.parse import quote

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from google_auth_oauthlib.flow import Flow
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session
from calendar_service import list_upcoming_events
from datetime import datetime
from pydantic import BaseModel
from calendar_service import create_calendar_event

from database import get_db
from models import GoogleCalendarConnection

from pathlib import Path

load_dotenv(
    dotenv_path=Path(__file__).resolve().parent / ".env",
    override=False,
)

router = APIRouter(prefix="/api/v1/calendar/google", tags=["Google Calendar"])

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
]

REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "http://localhost:8000/api/v1/calendar/google/callback",
)

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
STATE_SECRET = os.getenv("OAUTH_STATE_SECRET")
ENCRYPTION_KEY = os.getenv("CALENDAR_ENCRYPTION_KEY")
DEMO_BUSINESS_ID = os.getenv(
    "CALENDAR_DEMO_BUSINESS_ID", "demo-business"
)


def require_local_development(request: Request):
    """Reject this temporary integration outside local development."""
    if os.getenv("CALENDAR_LOCAL_DEV_ONLY", "").lower() != "true":
        raise HTTPException(
            status_code=503,
            detail="Google Calendar OAuth is disabled.",
        )

    if not request.client or request.client.host not in {
        "127.0.0.1", "::1"
    }:
        raise HTTPException(
            status_code=403,
            detail="This development endpoint is local-only.",
        )

    if not all([
        CLIENT_ID,
        CLIENT_SECRET,
        STATE_SECRET,
        ENCRYPTION_KEY,
    ]):
        raise HTTPException(
            status_code=500,
            detail="Google Calendar environment settings are missing.",
        )


def create_flow(state=None, code_verifier=None):
    client_config = {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=(code_verifier is None),
    )
    flow.redirect_uri = REDIRECT_URI
    return flow


def state_serializer():
    return URLSafeTimedSerializer(
        STATE_SECRET,
        salt="google-calendar-oauth-state",
    )



@router.get("/connect")
def connect_google_calendar(
    request: Request,
    db: Session = Depends(get_db),
):
    require_local_development(request)

    business_id = DEMO_BUSINESS_ID

    # Generate the verifier before creating the signed state.
    code_verifier = secrets.token_urlsafe(64)

    # Pass the same verifier to the OAuth flow.
    flow = create_flow(code_verifier=code_verifier)

    signed_state = state_serializer().dumps({
        "business_id": business_id,
        "nonce": secrets.token_urlsafe(24),
        "code_verifier": code_verifier,
    })
    print("CONNECT secret length:", len(STATE_SECRET or ""))
    print("CONNECT serializer salt:", "google-calendar-oauth-state")

    authorization_url, _ = flow.authorization_url(
    state=signed_state,
    access_type="offline",
    prompt="consent",
    include_granted_scopes="true",
    )

    print("Generated state length:", len(signed_state))

    return RedirectResponse(
        authorization_url,
        status_code=302,
    )



@router.get("/callback", response_class=HTMLResponse)
def google_calendar_callback(
    request: Request,
    state: str,
    code: str,
    db: Session = Depends(get_db),
):
    require_local_development(request)

    print("CALLBACK secret length:", len(STATE_SECRET or ""))
    print("CALLBACK serializer salt:", "google-calendar-oauth-state")
    print("Received state length:", len(state))

    try:
        payload = state_serializer().loads(state, max_age=600)
    except SignatureExpired:
        raise HTTPException(
            status_code=400,
            detail="OAuth state expired. Start a new connection.",
        )
    except BadSignature:
        raise HTTPException(
            status_code=400,
            detail=(
                "OAuth state signature is invalid. "
                "Check OAUTH_STATE_SECRET and the running backend."
            ),
        )

    if payload.get("business_id") != DEMO_BUSINESS_ID:
        raise HTTPException(
            status_code=400,
            detail="Unexpected demo business.",
        )

    code_verifier = payload.get("code_verifier")

    if not code_verifier:
        raise HTTPException(
            status_code=400,
            detail="PKCE verifier is missing. Start a new authorization attempt.",
        )

    flow = create_flow(
        state=state,
        code_verifier=code_verifier,
    )
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        print(
            f"Google OAuth token exchange failed: "
            f"{type(exc).__name__}: {exc}"
        )
        raise HTTPException(
            status_code=400,
            detail="Google authorization failed. Check the server terminal.",
        )

    credentials = flow.credentials
    refresh_token = credentials.refresh_token

    # Reauthorization may not return a refresh token. Preserve the
    # existing token in that case rather than overwriting it.
    connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id == DEMO_BUSINESS_ID
        )
        .first()
    )

    if not refresh_token:
        if connection is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Google did not return a refresh token. "
                    "Revoke this app's access in your Google Account "
                    "and connect again."
                ),
            )
    else:
        try:
            encrypted_token = Fernet(
                ENCRYPTION_KEY.encode()
            ).encrypt(refresh_token.encode()).decode()
        except Exception:
            raise HTTPException(
                status_code=500,
                detail="Could not encrypt the Google refresh token.",
            )

        if connection is None:
            connection = GoogleCalendarConnection(
                business_id=DEMO_BUSINESS_ID,
                calendar_id="primary",
                encrypted_refresh_token=encrypted_token,
                connected=True,
            )
            db.add(connection)
        else:
            connection.encrypted_refresh_token = encrypted_token
            connection.connected = True

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Could not save the calendar connection.",
        )

    return HTMLResponse(
        "<h2>Google Calendar connected successfully.</h2>"
        "<p>You can close this tab and return to FastAPI Swagger.</p>"
    )


@router.get("/status")
def google_calendar_status(
    request: Request,
    db: Session = Depends(get_db),
):
    require_local_development(request)

    connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id == DEMO_BUSINESS_ID
        )
        .first()
    )

    return {
        "business_id": DEMO_BUSINESS_ID,
        "connected": bool(connection and connection.connected),
        "calendar_id": (
            connection.calendar_id if connection else None
        ),
    }

@router.get("/events")
def google_calendar_events(
    request: Request,
    max_results: int = 10,
    db: Session = Depends(get_db),
):
    require_local_development(request)

    if not 1 <= max_results <= 50:
        raise HTTPException(
            status_code=400,
            detail="max_results must be between 1 and 50.",
        )

    events = list_upcoming_events(db, max_results)

    return {
        "count": len(events),
        "events": events,
    }

class CalendarEventCreate(BaseModel):
    summary: str
    start_at: datetime
    end_at: datetime
    description: str = ""


@router.post("/events")
def create_google_calendar_event(
    body: CalendarEventCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    require_local_development(request)

    return create_calendar_event(
        db=db,
        summary=body.summary,
        start_at=body.start_at,
        end_at=body.end_at,
        description=body.description,
    )
