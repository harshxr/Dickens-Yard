
from datetime import datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


AppointmentStatus = Literal[
    "pending",
    "confirmed",
    "cancelled",
    "completed",
]

BUSINESS_TIMEZONE = ZoneInfo("Europe/London")


def normalize_appointment_utc(value: datetime) -> datetime:
    """Require an explicit offset and normalize an appointment instant to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            "Appointment timestamps must include a timezone offset, such as +00:00 or Z."
        )
    return value.astimezone(timezone.utc)


def is_within_practice_hours_gmt(start_at: datetime, end_at: datetime) -> bool:
    """Check Monday-Saturday, 09:00-17:00 Europe/London local time."""
    start_utc = normalize_appointment_utc(start_at)
    end_utc = normalize_appointment_utc(end_at)

    start = start_utc.astimezone(BUSINESS_TIMEZONE)
    end = end_utc.astimezone(BUSINESS_TIMEZONE)

    return (
        end > start
        and start.date() == end.date()
        and start.weekday() < 6
        and start.time().replace(tzinfo=None) >= time(9)
        and end.time().replace(tzinfo=None) <= time(17)
    )


def calendar_gmt_datetime(value: datetime) -> dict[str, str]:
    """Build an offset-aware Google Calendar datetime in UTC."""
    utc_value = normalize_appointment_utc(value)
    return {"dateTime": utc_value.isoformat(), "timeZone": "UTC"}


# Compatibility aliases for existing callers.
is_within_practice_hours_london = is_within_practice_hours_gmt
calendar_london_datetime = calendar_gmt_datetime


class AppointmentCreate(BaseModel):
    business_id: str = Field(min_length=1, max_length=100)
    lead_id: int = Field(gt=0)
    service: str = Field(min_length=2, max_length=200)
    start_at: datetime
    end_at: datetime
    notes: str | None = None

    @model_validator(mode="after")
    def validate_appointment_times(self):
        self.start_at = normalize_appointment_utc(self.start_at)
        self.end_at = normalize_appointment_utc(self.end_at)

        if self.end_at <= self.start_at:
            raise ValueError("end_at must be later than start_at")

        if not is_within_practice_hours_gmt(self.start_at, self.end_at):
            raise ValueError(
                "Appointments must be Monday to Saturday, 09:00-17:00 "
                "Europe/London local time, and finish on the same London date."
            )

        return self


class AppointmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    business_id: str
    lead_id: int
    service: str
    start_at: datetime
    end_at: datetime
    status: AppointmentStatus
    notes: str | None
    created_at: datetime
