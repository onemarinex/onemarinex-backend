from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.port import Port
from app.db.models.port_rule import PortRule
from app.api.v1.routes_auth import get_current_user

router = APIRouter()

class RuleItem(BaseModel):
    title: str
    description: str
    icon_type: str # e.g., 'time', 'policy', 'doc', 'alert'

class PortRulesIn(BaseModel):
    rules: List[RuleItem]

class PortRulesOut(BaseModel):
    port_name: str
    rules: List[RuleItem]

    class Config:
        from_attributes = True

class PortOut(BaseModel):
    id: int
    name: str
    code: str

    class Config:
        from_attributes = True

@router.get("/", response_model=List[PortOut])
def get_ports(db: Session = Depends(get_db)):
    """Get list of active ports"""
    return db.query(Port).filter(Port.is_active == True).all()

@router.get("/{port_name}/rules", response_model=PortRulesOut)
def get_port_rules(port_name: str, db: Session = Depends(get_db)):
    rules = db.query(PortRule).filter(PortRule.port_name == port_name).first()
    if not rules:
        # Return empty rules instead of 404 to simplify frontend
        return {"port_name": port_name, "rules": []}
    return rules

@router.post("/{port_name}/rules", response_model=PortRulesOut)
def update_port_rules(
    port_name: str,
    body: PortRulesIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in ["agent", "aggregator"]:
        raise HTTPException(status_code=403, detail="Only agents can update port rules")

    # In a real scenario, we'd also check if the agent belongs to this port
    
    port_rules = db.query(PortRule).filter(PortRule.port_name == port_name).first()
    
    rule_data = [item.model_dump() for item in body.rules]
    
    if port_rules:
        port_rules.rules = rule_data
    else:
        port_rules = PortRule(port_name=port_name, rules=rule_data)
        db.add(port_rules)
    
    try:
        db.commit()
        db.refresh(port_rules)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
    return port_rules
