import csv

import httpx
from typer.testing import CliRunner

from nc_autotag import cli, pipeline
from nc_autotag.cli import app
from nc_autotag.state import State

runner = CliRunner()


def seeded(tmp_path):
    db = tmp_path / "s.db"
    config = tmp_path / "settings.toml"
    config.write_text(
        f'state_db = "{db.as_posix()}"\n[nextcloud]\nusers = ["alice"]\n', encoding="utf-8"
    )
    st = State(db)
    st.upsert_file("alice", 1, "/Photos/a.jpg", "image/jpeg", "e1", 1)
    st.upsert_file("alice", 2, "/Photos/a bis.jpg", "image/jpeg", "e2", 1)
    for i in (1, 2):
        st.mark_done("alice", i)
    run = st.start_run("apply")
    st.record_proposals("alice", 1, {"plage": 0.9})
    st.set_result("alice", 1, [], run)
    st.record_proposals("alice", 2, {"plage": 0.9})
    st.set_result("alice", 2, ["doublon"], run, keeper=1)
    st.journal_assignment(run, "alice", 1, "plage", 3)
    return st, config, run


def test_rollback_requires_yes_and_touches_nothing(tmp_path, monkeypatch):
    st, config, run = seeded(tmp_path)

    def boom(*a, **k):
        raise AssertionError("aucun appel réseau sans --yes")

    monkeypatch.setattr(pipeline, "rollback_assignments", boom)
    res = runner.invoke(app, ["rollback", "--run", str(run), "--config", str(config)])
    assert res.exit_code == 1
    assert "--yes" in res.output and "plage" in res.output
    assert len(st.all_assignments()) == 1


def test_rollback_yes_uses_only_journal(tmp_path, monkeypatch):
    st, config, run = seeded(tmp_path)
    monkeypatch.setenv("NC_AUTOTAG_PASSWORD_ALICE", "x")
    seen = []

    async def fake_rollback(state, assignments, make_client):
        seen.extend((a.fileid, a.tag) for a in assignments)
        state.mark_removed(assignments)
        from collections import Counter

        return Counter(removed=len(assignments))

    monkeypatch.setattr(pipeline, "rollback_assignments", fake_rollback)
    res = runner.invoke(app, ["rollback", "--yes", "--config", str(config)])
    assert res.exit_code == 0, res.output
    assert seen == [(1, "plage")]
    assert State(tmp_path / "s.db").all_assignments() == []
    assert State(tmp_path / "s.db").recent_runs()[0].mode == "rollback"


def test_rollback_nothing_to_do(tmp_path):
    st, config, _ = seeded(tmp_path)
    st.mark_removed(st.all_assignments())
    res = runner.invoke(app, ["rollback", "--run", "99", "--config", str(config)])
    assert res.exit_code == 0 and "Rien à retirer" in res.output


def test_status(tmp_path):
    _, config, _ = seeded(tmp_path)
    res = runner.invoke(app, ["status", "--config", str(config)])
    assert res.exit_code == 0
    assert "alice" in res.output and "apply" in res.output


def test_status_json(tmp_path):
    import json

    _, config, _ = seeded(tmp_path)
    res = runner.invoke(app, ["status", "--json", "--config", str(config)])
    assert res.exit_code == 0
    assert json.loads(res.output)["alice"]["done"] == 2


def test_report_csv_and_delete_candidates(tmp_path):
    _, config, run = seeded(tmp_path)
    out = tmp_path / "r.csv"
    res = runner.invoke(
        app, ["report", "--delete-candidates", "--csv", str(out), "--config", str(config)]
    )
    assert res.exit_code == 0, res.output
    lines = out.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 2
    row = next(csv.reader(lines[1:], delimiter=";"))
    assert row[3] == "à supprimer, plage"
    assert row[2] == "/Photos/a bis.jpg" and row[5] == "doublon" and row[6] == "/Photos/a.jpg"


def test_run_refuses_unknown_user_and_missing_credentials(tmp_path, monkeypatch):
    _, config, _ = seeded(tmp_path)
    assert runner.invoke(app, ["run", "--user", "x", "--config", str(config)]).exit_code == 2
    monkeypatch.delenv("NC_AUTOTAG_PASSWORD_ALICE", raising=False)
    res = runner.invoke(app, ["run", "--apply", "--config", str(config)])
    assert res.exit_code == 2 and "absent" in res.output


def test_inventory_read_only_and_idempotent(tmp_path, monkeypatch):
    _, config, _ = seeded(tmp_path)
    monkeypatch.setenv("NC_AUTOTAG_PASSWORD_ALICE", "x")
    methods = []
    from test_webdav import SEARCH_XML

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(207, text=SEARCH_XML)

    real = cli.NcClient

    def patched(*a, **kw):
        return real(*a, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(cli, "NcClient", patched)
    first = runner.invoke(app, ["inventory", "--config", str(config)])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["inventory", "--config", str(config)])
    assert set(methods) == {"SEARCH"}
    st = State(tmp_path / "s.db")
    assert st.get_file("alice", 5285).path == "/Photos/été 2021/IMG 001.jpg"
    assert "alice" in second.output


def test_parse_until_next_occurrence_and_midnight():
    from datetime import datetime

    now = datetime(2026, 11, 1, 22, 30)
    assert cli.parse_until("02:30", now) == datetime(2026, 11, 2, 2, 30)
    assert cli.parse_until("23:15", now) == datetime(2026, 11, 1, 23, 15)
    assert cli.parse_until("22:30", now) == datetime(2026, 11, 2, 22, 30)
    for bad in ("25:00", "abc", "2h"):
        try:
            cli.parse_until(bad, now)
        except Exception as e:
            assert "HH:MM" in str(e)
        else:
            raise AssertionError(bad)


def test_run_passes_until_exclude_and_no_delete_tag(tmp_path, monkeypatch):
    from collections import Counter
    from datetime import datetime

    _, config, _ = seeded(tmp_path)
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 11, 1, 23, 0))
    seen = {}

    async def fake_run(settings, state, d1, source, users, **kw):
        seen.update(kw)
        report = pipeline.RunReport(1, "dry-run", Counter())
        report.remaining, report.stopped = 7, True
        return report

    class FakeSource:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(pipeline, "run", fake_run)
    monkeypatch.setattr(cli, "SftpSource", FakeSource)
    res = runner.invoke(
        app,
        [
            "run",
            "--until",
            "02:30",
            "--no-delete-tag",
            "--exclude-path",
            "/Local",
            "--exclude-path",
            "/Autre",
            "--config",
            str(config),
        ],
    )
    assert res.exit_code == 0, res.output
    assert seen["stop_at"] == datetime(2026, 11, 2, 2, 30)
    assert seen["no_delete_tag"] is True and seen["exclude_paths"] == ["/Local", "/Autre"]
    assert "Reste en attente : 7" in res.output and "arrêt propre" in res.output
    bad = runner.invoke(app, ["run", "--until", "99:99", "--config", str(config)])
    assert bad.exit_code == 2


def test_config_show_json_has_no_secret(tmp_path, monkeypatch):
    import json

    _, config, _ = seeded(tmp_path)
    monkeypatch.setenv("NC_AUTOTAG_PASSWORD_ALICE", "tres-secret")
    res = runner.invoke(app, ["config-show", "--json", "--config", str(config)])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["llama"]["port"] == 8080 and data["users"] == ["alice"]
    assert "tres-secret" not in res.output and "password" not in res.output.lower()
