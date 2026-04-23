from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from meshonator.db.models import UserModel
from meshonator.db.session import get_db

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


@dataclass
class CurrentUser:
    username: str
    role: str


ROLE_ORDER = {"viewer": 1, "operator": 2, "admin": 3}


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


def authenticate_user(db: Session, username: str, password: str) -> CurrentUser | None:
    user = db.scalar(select(UserModel).where(UserModel.username == username, UserModel.is_active.is_(True)))
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return CurrentUser(username=user.username, role=user.role)


def require_session_user(request: Request) -> CurrentUser:
    user = request.session.get("user") if hasattr(request, "session") else None
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return CurrentUser(username=user["username"], role=user["role"])


def require_role(min_role: str):
    def checker(user: CurrentUser = Depends(require_session_user)) -> CurrentUser:
        if ROLE_ORDER[user.role] < ROLE_ORDER[min_role]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return checker


def bootstrap_admin(db: Session, username: str, password: str, role: str) -> None:
    existing = db.scalar(select(UserModel).where(UserModel.username == username))
    if existing:
        return
    db.add(UserModel(username=username, password_hash=hash_password(password), role=role, is_active=True))
    db.commit()
