import httpx
from typer.testing import CliRunner

from nc_autotag.cli import app
from nc_autotag.config import Settings
from nc_autotag.doctor import FAIL, OK, SKIP, check_credentials, check_files, check_webdav


def _settings():
    s = Settings()
    s.nextcloud.users = ["alice"]
    return s


def test_credentials(monkeypatch):
    monkeypatch.setenv("NC_AUTOTAG_PASSWORD_ALICE", "x")
    assert check_credentials(_settings())[0].status == OK
    monkeypatch.delenv("NC_AUTOTAG_PASSWORD_ALICE")
    assert check_credentials(_settings())[0].status == FAIL


def test_webdav_ok(monkeypatch):
    monkeypatch.setenv("NC_AUTOTAG_PASSWORD_ALICE", "x")
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["depth"] = request.headers["Depth"]
        seen["path"] = request.url.path
        return httpx.Response(207)

    r = check_webdav(_settings(), httpx.MockTransport(handler))[0]
    assert r.status == OK
    assert seen == {"method": "PROPFIND", "depth": "0", "path": "/remote.php/dav/files/alice/"}


def test_webdav_401(monkeypatch):
    monkeypatch.setenv("NC_AUTOTAG_PASSWORD_ALICE", "x")
    r = check_webdav(_settings(), httpx.MockTransport(lambda req: httpx.Response(401)))[0]
    assert r.status == FAIL
    assert "401" in r.message


def test_webdav_network_error(monkeypatch):
    monkeypatch.setenv("NC_AUTOTAG_PASSWORD_ALICE", "x")

    def handler(request):
        raise httpx.ConnectError("down")

    r = check_webdav(_settings(), httpx.MockTransport(handler))[0]
    assert r.status == FAIL
    assert "ConnectError" in r.message


def test_webdav_no_credentials(monkeypatch):
    monkeypatch.delenv("NC_AUTOTAG_PASSWORD_ALICE", raising=False)
    r = check_webdav(_settings(), httpx.MockTransport(lambda req: httpx.Response(207)))[0]
    assert r.status == SKIP


def test_files_missing(tmp_path):
    s = _settings()
    s.llama.server_exe = tmp_path / "nope.exe"
    s.llama.model = tmp_path / "m.gguf"
    s.llama.mmproj = tmp_path / "p.gguf"
    s.llama.model.write_text("x")
    statuses = [r.status for r in check_files(s)]
    assert statuses == [FAIL, OK, FAIL]


def test_cli_exit_code_and_only(monkeypatch, tmp_path):
    monkeypatch.delenv("NC_AUTOTAG_PASSWORD_ALICE", raising=False)
    monkeypatch.delenv("NC_AUTOTAG_PASSWORD_BOB", raising=False)
    runner = CliRunner()
    res = runner.invoke(app, ["doctor", "--only", "credentials"])
    assert res.exit_code == 1
    assert "FAIL" in res.output
    monkeypatch.setenv("NC_AUTOTAG_PASSWORD_ALICE", "x")
    monkeypatch.setenv("NC_AUTOTAG_PASSWORD_BOB", "x")
    res = runner.invoke(app, ["doctor", "--only", "credentials"])
    assert res.exit_code == 0
    assert runner.invoke(app, ["doctor", "--only", "bogus"]).exit_code == 2
