from typing import Optional
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.user import User
from app.services.auth import decode_subject 


from fastapi import Depends, Header, HTTPException, status
# ... existing imports ...

def get_current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token = authorization.split()[1]
    email = decode_subject(token)
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user

def get_current_driver(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> any:
    from app.db.models.driver import Driver
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    token = authorization.split()[1]
    email = decode_subject(token)
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    driver = db.query(Driver).filter(Driver.email == email).first()
    if not driver:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Driver not found")
    return driver
