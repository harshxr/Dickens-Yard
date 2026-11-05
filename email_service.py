import os
import html
from datetime import datetime, timezone

import resend


def send_appointment_email(
    to_email: str,
    patient_name: str,
    service: str,
    start_at: datetime,
    notification_type: str,
):
    api_key = os.getenv("RESEND_API_KEY")
    sender = os.getenv("EMAIL_FROM")
    reply_to = os.getenv("EMAIL_REPLY_TO")

    if not api_key or not sender:
        raise RuntimeError("RESEND_API_KEY or EMAIL_FROM is not configured.")

    if notification_type not in {"confirmation", "reminder_24h"}:
        raise ValueError("Unsupported appointment notification type.")

    if start_at.tzinfo is None:
        raise ValueError("Appointment start_at must be timezone-aware.")

    appointment_time = start_at.astimezone(timezone.utc)
    formatted_time = appointment_time.strftime("%A, %d %B %Y at %H:%M GMT")

    safe_name = html.escape(patient_name or "Patient")
    safe_service = html.escape(service or "Dental appointment")

    if notification_type == "confirmation":
        subject = "Your dental appointment is confirmed"
        heading = "Your appointment is confirmed"
        message = "Your appointment has been successfully booked."
    else:
        subject = "Reminder: Your dental appointment is tomorrow"
        heading = "Your appointment reminder"
        message = "This is a reminder about your upcoming dental appointment."

    html_body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333;">
        <h2>{heading}</h2>
        <p>Hello {safe_name},</p>
        <p>{message}</p>
        <p><strong>Practice:</strong> Dickens Yard Dental Ealing</p>
        <p><strong>Service:</strong> {safe_service}</p>
        <p><strong>Date and time:</strong> {formatted_time}</p>
        <p>If you need to change your appointment, please contact the practice.</p>
        <p>Kind regards,<br>Reception Team<br> Dickens Yard Dental Ealing</p>
      </body>
    </html>
    """

    text_body = (
        f"Hello {patient_name or 'Patient'},\n\n"
        f"{message}\n"
        "Practice: Dickens Yard Dental Ealing\n"
        f"Service: {service or 'Dental appointment'}\n"
        f"Date and time: {formatted_time}\n\n"
        "If you need to change your appointment, please contact the practice.\n\n"
        "Kind regards,\nReception Team\nDickens Yard Dental Ealing"
    )

    resend.api_key = api_key

    email_data = {
        "from": sender,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }

    if reply_to:
        email_data["reply_to"] = reply_to

    return resend.Emails.send(email_data)
