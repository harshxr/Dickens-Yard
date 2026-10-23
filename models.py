
# pyright: reportMissingImports=false
# pyright: reportMissingImports=false
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
)

from database import Base

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    phone = Column(String(20), nullable=False)
    email = Column(String(255), nullable=True)
    interest = Column(String(200), nullable=False)
    status = Column(String(20), nullable=False, default="new")


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(String(100), nullable=False, index=True)
    lead_id = Column(
        Integer,
        ForeignKey("leads.id"),
        nullable=False,
        index=True,
    )
    service = Column(String(200), nullable=False)
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default="confirmed")
    notes = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
    )


class CallbackTask(Base):
    __tablename__ = "callback_tasks"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(String(100), nullable=False, index=True)
    lead_id = Column(
        Integer,
        ForeignKey("leads.id"),
        nullable=False,
        index=True,
    )
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default="scheduled")
    assigned_to = Column(String(150), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
    )
class GoogleCalendarConnection(Base):
    __tablename__ = "google_calendar_connections"

    id = Column(Integer, primary_key=True, index=True)

    business_id = Column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
    )

    calendar_id = Column(
        String(255),
        nullable=False,
        default="primary",
    )

    encrypted_refresh_token = Column(Text, nullable=False)

    connected = Column(Boolean, nullable=False, default=True)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
    )

    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )