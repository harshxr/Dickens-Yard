
import json
import logging
import os
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def sync_customer_to_google_sheet(
    phone: str,
    customer_name: str = "",
    enquiry_service: str = "",
    appointment_date: str = "",
    appointment_time: str = "",
    booking_status: str = "",
    call_outcome: str = "",
):
    """Send customer and appointment details to Google Sheets."""

    url = os.getenv("GOOGLE_SHEETS_WEBHOOK_URL")
    token = os.getenv("GOOGLE_SHEETS_WEBHOOK_TOKEN")

    if not url or not token:
        logger.warning("Google Sheets integration is not configured.")
        return False

    if not phone:
        logger.warning("Google Sheets sync skipped: missing phone.")
        return False

    payload = {
        "token": token,
        "phone": phone,
        "customer_name": customer_name,
        "enquiry_service": enquiry_service,
        "appointment_date": appointment_date,
        "appointment_time": appointment_time,
        "booking_status": booking_status,
        "call_outcome": call_outcome,
    }

    try:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))

        if result.get("success"):
            logger.info("Google Sheets sync successful.")
            return True

        logger.error(
            "Google Sheets rejected sync: %s",
            result.get("message", "Unknown error")
        )
        return False

    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        logger.exception("Google Sheets sync failed.")
        return False
