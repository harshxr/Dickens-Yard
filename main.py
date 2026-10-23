
# pyright: reportMissingImports=false

from datetime import timezone

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.orm import Session

from calendar_oauth import router as google_calendar_router

from database import Base, engine, get_db
from models import (
    Lead,
    Appointment,
    CallbackTask,
    GoogleCalendarConnection,
)

from schemas.lead import LeadCreate, LeadResponse, LeadUpdate
from schemas.appointment import AppointmentCreate, AppointmentResponse
from schemas.callback import (
    CallbackCreate,
    CallbackResponse,
    CallbackStatusUpdate,
)

# Create database tables if they do not already exist
Base.metadata.create_all(bind=engine)


app = FastAPI(
    title="Voice Agent Platform API",
    description="Backend for managing AI voice agents, leads, appointments, and callbacks",
    version="1.0.0",
)
app.include_router(google_calendar_router)

# --------------------------------------------------
# HOME AND HEALTH CHECK
# --------------------------------------------------

@app.get("/")
def home():
    return {"message": "Voice Agent Platform API is running"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/api/v1")
def api_info():
    return {
        "name": "Voice Agent Platform",
        "version": "1.0.0",
        "features": [
            "Lead management",
            "Voice agent integration",
            "Call management",
            "Appointment booking",
            "Callback management",
        ],
    }


# --------------------------------------------------
# LEAD MANAGEMENT
# --------------------------------------------------

@app.post(
    "/api/v1/leads",
    response_model=LeadResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_lead(
    lead: LeadCreate,
    db: Session = Depends(get_db),
):
    new_lead = Lead(**lead.model_dump())

    try:
        db.add(new_lead)
        db.commit()
        db.refresh(new_lead)
        return new_lead
    except Exception:
        db.rollback()
        raise


@app.get(
    "/api/v1/leads",
    response_model=list[LeadResponse],
)
def get_all_leads(db: Session = Depends(get_db)):
    return db.query(Lead).order_by(Lead.id).all()


@app.get(
    "/api/v1/leads/{lead_id}",
    response_model=LeadResponse,
)
def get_lead(
    lead_id: int,
    db: Session = Depends(get_db),
):
    lead = db.get(Lead, lead_id)

    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )

    return lead


@app.put(
    "/api/v1/leads/{lead_id}",
    response_model=LeadResponse,
)
def update_lead(
    lead_id: int,
    updates: LeadUpdate,
    db: Session = Depends(get_db),
):
    lead = db.get(Lead, lead_id)

    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )

    changes = updates.model_dump(exclude_unset=True)

    for key, value in changes.items():
        setattr(lead, key, value)

    try:
        db.commit()
        db.refresh(lead)
        return lead
    except Exception:
        db.rollback()
        raise


@app.delete("/api/v1/leads/{lead_id}")
def delete_lead(
    lead_id: int,
    db: Session = Depends(get_db),
):
    lead = db.get(Lead, lead_id)

    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )

    try:
        db.delete(lead)
        db.commit()
        return {
            "message": f"Lead {lead_id} deleted successfully"
        }
    except Exception:
        db.rollback()
        raise


# --------------------------------------------------
# APPOINTMENT MANAGEMENT
# --------------------------------------------------

@app.post(
    "/api/v1/appointments",
    response_model=AppointmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_appointment(
    appointment: AppointmentCreate,
    db: Session = Depends(get_db),
):
    # 1. Verify that the lead exists
    lead = db.get(Lead, appointment.lead_id)

    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )

    # 2. Convert timezone-aware timestamps to UTC
    # appointment.py validates that both timestamps include a timezone.
    start_at = appointment.start_at.astimezone(timezone.utc)
    end_at = appointment.end_at.astimezone(timezone.utc)

    # 3. Find an existing appointment that overlaps this time.
    # Cancelled appointments do not block a booking.
    overlapping_appointment = (
        db.query(Appointment)
        .filter(
            Appointment.business_id == appointment.business_id,
            Appointment.status != "cancelled",
            Appointment.start_at < end_at,
            Appointment.end_at > start_at,
        )
        .first()
    )

    if overlapping_appointment is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This business already has an appointment "
                "during the requested time."
            ),
        )

    # 4. Create the appointment
    new_appointment = Appointment(
        **appointment.model_dump(
            exclude={"start_at", "end_at"}
        ),
        start_at=start_at,
        end_at=end_at,
    )

    try:
        db.add(new_appointment)
        db.commit()
        db.refresh(new_appointment)
        return new_appointment
    except Exception:
        db.rollback()
        raise


@app.get(
    "/api/v1/appointments",
    response_model=list[AppointmentResponse],
)
def get_all_appointments(db: Session = Depends(get_db)):
    return db.query(Appointment).order_by(Appointment.id).all()


@app.get(
    "/api/v1/appointments/{appointment_id}",
    response_model=AppointmentResponse,
)
def get_appointment(
    appointment_id: int,
    db: Session = Depends(get_db),
):
    appointment = db.get(Appointment, appointment_id)

    if appointment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Appointment not found",
        )

    return appointment


# --------------------------------------------------
# CALLBACK MANAGEMENT
# --------------------------------------------------

@app.post(
    "/api/v1/callbacks",
    response_model=CallbackResponse,
    status_code=status.HTTP_201_CREATED,
)

def create_callback(
    task: CallbackCreate,
    db: Session = Depends(get_db),
):
    # Verify that the lead exists
    lead = db.get(Lead, task.lead_id)

    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )

    new_callback = CallbackTask(**task.model_dump())

    try:
        db.add(new_callback)
        db.commit()
        db.refresh(new_callback)
        return new_callback
    except Exception:
        db.rollback()
        raise


@app.get(
    "/api/v1/callbacks",
    response_model=list[CallbackResponse],
)
def get_all_callbacks(db: Session = Depends(get_db)):
    return db.query(CallbackTask).order_by(CallbackTask.id).all()


@app.get(
    "/api/v1/callbacks/{callback_id}",
    response_model=CallbackResponse,
)
def get_callback(
    callback_id: int,
    db: Session = Depends(get_db),
):
    callback = db.get(CallbackTask, callback_id)

    if callback is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Callback task not found",
        )

    return callback


@app.patch(
    "/api/v1/callbacks/{callback_id}/status",
    response_model=CallbackResponse,
)
def update_callback_status(
    callback_id: int,
    status_update: CallbackStatusUpdate,
    db: Session = Depends(get_db),
):
    callback = db.get(CallbackTask, callback_id)

    if callback is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Callback task not found",
        )

    changes = status_update.model_dump(exclude_unset=True)

    for key, value in changes.items():
        setattr(callback, key, value)

    try:
        db.commit()
        db.refresh(callback)
        return callback
    except Exception:
        db.rollback()
        raise
