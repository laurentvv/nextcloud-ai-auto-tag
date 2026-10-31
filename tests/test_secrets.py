from pydantic import SecretStr

from nc_autotag.secrets import get_app_password, target_name


def test_target_name():
    assert target_name("alice") == "nc-autotag:alice"


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("NC_AUTOTAG_PASSWORD_ALICE", "s3cret")
    pw = get_app_password("alice")
    assert isinstance(pw, SecretStr)
    assert pw.get_secret_value() == "s3cret"
    assert "s3cret" not in repr(pw)


def test_missing(monkeypatch):
    monkeypatch.delenv("NC_AUTOTAG_PASSWORD_BOB", raising=False)
    assert get_app_password("bob") is None
