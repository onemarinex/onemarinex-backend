from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models.port import Port

router = APIRouter()

class PortOut(BaseModel):
    id: int
    name: str
    code: str

    class Config:
        from_attributes = True

@router.get("/", response_model=List[PortOut])
def get_ports(db: Session = Depends(get_db)):
    """Retrieve all active ports."""
    return db.query(Port).filter(Port.is_active == True).order_by(Port.name).all()
