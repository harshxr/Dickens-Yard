
import os
import secrets
import hmac
import logging
from urllib.parse import quote

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, Header
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
logger = logging.getLogger(__name__)

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


def require_calendar_configured():
    """Verify that Google Calendar OAuth secrets are configured."""
    if not all([
        CLIENT_ID,
        CLIENT_SECRET,
        STATE_SECRET,
        ENCRYPTION_KEY,
        REDIRECT_URI,
    ]):
        raise HTTPException(
            status_code=503,
            detail="Google Calendar environment settings are missing.",
        )


def require_calendar_admin(
    authorization: str | None = Header(default=None),
):
    """Authenticate calendar administration using a dedicated bearer token."""
    configured_token = os.getenv("CALENDAR_ADMIN_TOKEN", "")

    if len(configured_token) < 32:
        raise HTTPException(
            status_code=503,
            detail="Calendar administration is not configured.",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Calendar admin authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    supplied_token = authorization[7:]

    if not hmac.compare_digest(supplied_token, configured_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid calendar admin credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    require_calendar_configured()


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
    db: Session = Depends(get_db),
    admin: None = Depends(require_calendar_admin),
):

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
    authorization_url, _ = flow.authorization_url(
    state=signed_state,
    access_type="offline",
    prompt="consent",
    include_granted_scopes="true",
    )

    return RedirectResponse(
        authorization_url,
        status_code=302,
    )



@router.get("/callback", response_class=HTMLResponse)
def google_calendar_callback(
    state: str,
    code: str,
    db: Session = Depends(get_db),
):
    require_calendar_configured()

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
        logger.warning("Google OAuth token exchange failed (%s).", type(exc).__name__)
        raise HTTPException(
            status_code=400,
            detail="Google authorization failed. Check the local OAuth configuration and try again.",
        ) from None

    credentials = flow.credentials
    refresh_token = credentials.refresh_token

    connection = (
        db.query(GoogleCalendarConnection)
        .filter(
            GoogleCalendarConnection.business_id == DEMO_BUSINESS_ID
        )
        .first()
    )

    # Only a newly issued refresh token can restore a connection. Keep any
    # existing ciphertext, but do not leave it marked healthy after this attempt.
    if not refresh_token:
        if connection is not None:
            connection.connected = False
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise HTTPException(
                    status_code=500,
                    detail="Could not update the Google Calendar connection state.",
                ) from None
        raise HTTPException(
            status_code=400,
            detail=(
                "Google did not issue a refresh token. Existing credentials were preserved "
                "and the connection requires reconnection. Revoke this app's access in "
                "your Google Account, then reconnect with consent."
            ),
        )

    try:
        encrypted_token = Fernet(
            ENCRYPTION_KEY.encode()
        ).encrypt(refresh_token.encode()).decode()
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Could not securely store the Google authorization. No connection was changed.",
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
    db: Session = Depends(get_db),
    admin: None = Depends(require_calendar_admin),
):

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
    max_results: int = 10,
    db: Session = Depends(get_db),
    admin: None = Depends(require_calendar_admin),
):

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
    db: Session = Depends(get_db),
    admin: None = Depends(require_calendar_admin),
):

    return create_calendar_event(
        db=db,
        summary=body.summary,
        start_at=body.start_at,
        end_at=body.end_at,
        description=body.description,
    )
