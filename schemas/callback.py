
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CallbackStatus = Literal[
    "scheduled",
    "in_progress",
    "completed",
    "cancelled",
]


class CallbackCreate(BaseModel):
    business_id: str = Field(min_length=1, max_length=100)
    lead_id: int = Field(gt=0)
    scheduled_at: datetime
    assigned_to: str | None = Field(default=None, max_length=150)
    notes: str | None = None


class CallbackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    business_id: str
    lead_id: int
    scheduled_at: datetime
    status: CallbackStatus
    assigned_to: str | None
    notes: str | None
    created_at: datetime


class CallbackStatusUpdate(BaseModel):
    status: Literal[
        "scheduled",
        "in_progress",
        "completed",
        "cancelled",
    ]
