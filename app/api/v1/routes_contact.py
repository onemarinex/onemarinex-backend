from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.contact_message import ContactMessage

router = APIRouter()

from pydantic import BaseModel

class ContactIn(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str
    message: str

@router.post("/")
def contact(body: ContactIn, db: Session = Depends(get_db)):
    db_msg = ContactMessage(
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        phone=body.phone,
        message=body.message
    )
    db.add(db_msg)
    db.commit()
    db.refresh(db_msg)
    return {"message": f"Thanks {first_name}, we received your message."}
