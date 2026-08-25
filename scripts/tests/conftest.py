from zoneinfo import ZoneInfo

import pytest

import db

UTC = ZoneInfo("UTC")


@pytest.fixture
def conn(tmp_path):
    """A fresh, temporary SQLite DB per test - never touches data/tracker.db."""
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()
