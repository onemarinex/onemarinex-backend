from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.contact_message import ContactMessage

router = APIRouter()

@router.post("/")
def contact(first_name: str, last_name: str, email: str, phone: str, message: str, db: Session = Depends(get_db)):
    db_msg = ContactMessage(
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        message=message
    )
    db.add(db_msg)
    db.commit()
    db.refresh(db_msg)
    return {"message": f"Thanks {first_name}, we received your message."}
