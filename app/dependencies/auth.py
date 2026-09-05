from uuid import UUID
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.database import get_db
from app.models.user import User

security_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency to extract, validate JWT token, and return authenticated user."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception

    user_identifier: Optional[str] = payload.get("sub")
    if not user_identifier:
        raise credentials_exception

    # Find user by UUID id, or fall back to email
    user = None
    try:
        user_uuid = UUID(user_identifier)
        user = db.query(User).filter(User.id == user_uuid).first()
    except (ValueError, AttributeError):
        user = db.query(User).filter(User.email == user_identifier).first()

    if user is None:
        raise credentials_exception

    return user
