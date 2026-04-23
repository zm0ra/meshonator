from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///./test_meshonator.db"
os.environ["APP_ENV"] = "test"
os.environ["SCHEDULER_ENABLED"] = "false"

from meshonator.api.app import app  # noqa: E402
from meshonator.db.base import Base  # noqa: E402
from meshonator.db.session import get_db  # noqa: E402

engine = create_engine("sqlite+pysqlite:///./test_meshonator.db", future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture(scope="session", autouse=True)
def create_schema():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client(db: Session):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
