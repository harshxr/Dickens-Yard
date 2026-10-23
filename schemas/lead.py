
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, EmailStr

LeadStatus = Literal["new", "contacted", "qualified", "converted", "lost"]


class LeadCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=7, max_length=20)
    email: EmailStr | None = None
    interest: str = Field(min_length=2, max_length=200)
    status: LeadStatus = "new"


class LeadUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    phone: str | None = Field(default=None, min_length=7, max_length=20)
    email: EmailStr | None = None
    interest: str | None = Field(default=None, min_length=2, max_length=200)
    status: LeadStatus | None = None


class LeadResponse(LeadCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
