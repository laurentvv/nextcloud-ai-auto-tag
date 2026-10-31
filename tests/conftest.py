import pytest

from nc_autotag import secrets


@pytest.fixture(autouse=True)
def no_credential_manager(monkeypatch):
    monkeypatch.setattr(secrets, "_read_credential_manager", lambda target: None)
