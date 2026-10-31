from nc_autotag.config import load_settings


def test_defaults_without_file(tmp_path):
    s = load_settings(tmp_path / "absent.toml")
    assert s.nextcloud.users == ["alice", "bob"]
    assert s.ssh.data_dir == "/var/www/html/nextcloud/data"
    assert s.llama.port == 8080
    assert s.llama.image_max_side == 768


def test_toml_override(tmp_path):
    f = tmp_path / "settings.toml"
    f.write_text(
        '[nextcloud]\nbase_url = "https://x.test"\nusers = ["a"]\n'
        '[ssh]\ndata_dir = "/srv/nc/data"\n[llama]\nport = 9090\n',
        encoding="utf-8",
    )
    s = load_settings(f)
    assert s.nextcloud.base_url == "https://x.test"
    assert s.nextcloud.users == ["a"]
    assert s.ssh.data_dir == "/srv/nc/data"
    assert s.ssh.host == "192.168.1.10"
    assert s.llama.port == 9090


def test_example_file_loads():
    s = load_settings(__import__("pathlib").Path("config/settings.example.toml"))
    assert s.llama.ub == 8192


def test_quality_defaults_and_override(tmp_path):
    s = load_settings(tmp_path / "absent.toml")
    assert s.delete_tag_name == "à supprimer"
    assert (s.quality.blur_var_threshold, s.quality.dup_max_hamming) == (30.0, 4)
    assert s.quality.dup_window_minutes == 30
    f = tmp_path / "s.toml"
    f.write_text('delete_tag_name = "x"\n[quality]\nblur_var_threshold = 12.5\n', encoding="utf-8")
    s = load_settings(f)
    assert s.delete_tag_name == "x" and s.quality.blur_var_threshold == 12.5
