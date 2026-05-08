from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.early_access import EarlyAccess

router = APIRouter()

from pydantic import BaseModel

class EarlyAccessIn(BaseModel):
    email: str

@router.post("/")
def early_access(body: EarlyAccessIn, db: Session = Depends(get_db)):
    db_entry = EarlyAccess(email=body.email)
    db.add(db_entry)
    db.commit()
    db.refresh(db_entry)
    return {"message": "Thank you for joining our early access list!"}
