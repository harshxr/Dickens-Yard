
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AppointmentStatus = Literal[
    "confirmed",
    "cancelled",
    "completed",
]


class AppointmentCreate(BaseModel):
    business_id: str = Field(min_length=1, max_length=100)
    lead_id: int = Field(gt=0)
    service: str = Field(min_length=2, max_length=200)
    start_at: datetime
    end_at: datetime
    notes: str | None = None

    @model_validator(mode="after")
    def validate_appointment_times(self):
        if self.end_at <= self.start_at:
            raise ValueError(
                "end_at must be later than start_at"
            )

        if (
            self.start_at.tzinfo is None
            or self.start_at.utcoffset() is None
            or self.end_at.tzinfo is None
            or self.end_at.utcoffset() is None
        ):
            raise ValueError(
                "start_at and end_at must include a timezone"
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
