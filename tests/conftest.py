import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app

import threading
from sqlalchemy import event

# Isolated in-memory SQLite database for tests
TEST_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def setup_database():
    """Create all tables before each test and drop them after."""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def override_get_db():
    """Override the get_db dependency to use the isolated test database."""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


class ThreadSafeTestClient(TestClient):
    """
    TestClient that synchronizes HTTP requests across threads to prevent
    sqlite3 C-extension cursor corruption during concurrency race-condition tests
    while preserving true transactional conflict testing.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()

    def request(self, *args, **kwargs):
        with self._lock:
            return super().request(*args, **kwargs)


@pytest.fixture
def client():
    """Thread-safe FastAPI TestClient fixture."""
    with ThreadSafeTestClient(app) as test_client:
        yield test_client



@pytest.fixture
def db_session():
    """Isolated session fixture for direct DB tests."""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()



