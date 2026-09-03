"""Tests for lunchmenu.config: resolution order, coercion, state_dir()."""

from __future__ import annotations

from lunchmenu import config


def test_missing_config_file_is_not_an_error(monkeypatch, tmp_path):
    """LUNCHMENU_CONFIG pointing at a file that doesn't exist -- the normal
    case for a fresh checkout -- must not raise."""
    monkeypatch.setenv("LUNCHMENU_CONFIG", str(tmp_path / "does-not-exist.toml"))
    resolved = config.reload()
    assert resolved["school"]["identifier"] == ""  # built-in default, untouched


def test_builtin_default_when_nothing_set():
    resolved = config.reload()
    assert resolved["web"]["port"] == 8090
    assert resolved["speaker"]["volume"] is None
    assert resolved["school"]["timezone"] == "America/New_York"


def test_config_file_beats_builtin_default(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[school]\nidentifier = "from-file"\n')
    monkeypatch.setenv("LUNCHMENU_CONFIG", str(cfg))
    resolved = config.reload()
    assert resolved["school"]["identifier"] == "from-file"


def test_env_var_beats_config_file(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[school]\nidentifier = "from-file"\n')
    monkeypatch.setenv("LUNCHMENU_CONFIG", str(cfg))
    monkeypatch.setenv("LUNCHMENU_SCHOOL_IDENTIFIER", "from-env")
    resolved = config.reload()
    assert resolved["school"]["identifier"] == "from-env"


def test_env_var_beats_config_file_and_config_file_beats_default_together(monkeypatch, tmp_path):
    """Precedence chain end to end: three keys, three different sources,
    each key resolving from the highest-priority source that set it."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('[school]\nidentifier = "from-file"\nname = "File Elementary"\n')
    monkeypatch.setenv("LUNCHMENU_CONFIG", str(cfg))
    monkeypatch.setenv("LUNCHMENU_SCHOOL_IDENTIFIER", "from-env")
    # district_id is set nowhere -- must fall through to the built-in default.
    resolved = config.reload()
    assert resolved["school"]["identifier"] == "from-env"  # env
    assert resolved["school"]["name"] == "File Elementary"  # file
    assert resolved["school"]["district_id"] == ""  # built-in default


def test_cwd_config_toml_is_ignored_entirely(monkeypatch, tmp_path):
    """Regression test for the hardening in _candidate_files(): a
    ./config.toml sitting in the current directory (e.g. a repo clone) must
    NEVER be read, even when nothing else is configured. This is the
    invariant most likely to be "helpfully" reverted later by someone
    restoring the old cwd-fallback behavior -- don't re-add
    Path("config.toml") to _candidate_files().
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "config.toml").write_text('[school]\nidentifier = "from-cwd-must-not-be-read"\n')

    monkeypatch.chdir(cwd)
    monkeypatch.delenv("LUNCHMENU_CONFIG", raising=False)
    resolved = config.reload()
    assert resolved["school"]["identifier"] == ""  # built-in default, not the cwd file


def test_lunchmenu_config_beats_config_dir(monkeypatch, tmp_path):
    """Among config *files* (no env var override for individual keys),
    $LUNCHMENU_CONFIG outranks ~/.config/lunchmenu/config.toml -- see
    config.py's _candidate_files ordering."""
    config_dir_cfg = tmp_path / "config-dir"
    config_dir_cfg.mkdir()
    (config_dir_cfg / "config.toml").write_text('[school]\nidentifier = "from-config-dir"\n')

    explicit_cfg = tmp_path / "explicit-config.toml"
    explicit_cfg.write_text('[school]\nidentifier = "from-explicit-path"\n')

    monkeypatch.setenv("LUNCHMENU_CONFIG_DIR", str(config_dir_cfg))
    monkeypatch.setenv("LUNCHMENU_CONFIG", str(explicit_cfg))
    resolved = config.reload()
    assert resolved["school"]["identifier"] == "from-explicit-path"


def test_config_dir_beats_builtin_default(monkeypatch, tmp_path):
    config_dir_cfg = tmp_path / "config-dir"
    config_dir_cfg.mkdir()
    (config_dir_cfg / "config.toml").write_text('[school]\nidentifier = "from-config-dir"\n')

    monkeypatch.setenv("LUNCHMENU_CONFIG_DIR", str(config_dir_cfg))
    monkeypatch.delenv("LUNCHMENU_CONFIG", raising=False)
    resolved = config.reload()
    assert resolved["school"]["identifier"] == "from-config-dir"


def test_type_coercion_web_port_int(monkeypatch):
    monkeypatch.setenv("LUNCHMENU_WEB_PORT", "9999")
    resolved = config.reload()
    assert resolved["web"]["port"] == 9999
    assert isinstance(resolved["web"]["port"], int)


def test_type_coercion_speaker_volume_float(monkeypatch):
    monkeypatch.setenv("LUNCHMENU_SPEAKER_VOLUME", "0.65")
    resolved = config.reload()
    assert resolved["speaker"]["volume"] == 0.65
    assert isinstance(resolved["speaker"]["volume"], float)


def test_type_coercion_from_config_file(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[web]\nport = 7000\n[speaker]\nvolume = 0.3\n")
    monkeypatch.setenv("LUNCHMENU_CONFIG", str(cfg))
    resolved = config.reload()
    assert resolved["web"]["port"] == 7000
    assert isinstance(resolved["web"]["port"], int)
    assert resolved["speaker"]["volume"] == 0.3
    assert isinstance(resolved["speaker"]["volume"], float)


def test_get_reads_through_cache(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[school]\nname = "First"\n')
    monkeypatch.setenv("LUNCHMENU_CONFIG", str(cfg))
    config.reload()
    assert config.get("school", "name") == "First"

    cfg.write_text('[school]\nname = "Second"\n')
    # get() alone must NOT reread the file -- config is cached until reload().
    assert config.get("school", "name") == "First"
    config.reload()
    assert config.get("school", "name") == "Second"


def test_state_dir_respects_env_var(monkeypatch, tmp_path):
    target = tmp_path / "custom-state"
    monkeypatch.setenv("LUNCHMENU_STATE_DIR", str(target))
    resolved = config.state_dir()
    assert resolved == target
    assert target.is_dir()
    assert (target.stat().st_mode & 0o777) == 0o700


def test_state_dir_defaults_under_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.delenv("LUNCHMENU_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    resolved = config.state_dir()
    assert resolved == tmp_path / "xdg" / "lunchmenu"


def test_config_dir_respects_env_var(monkeypatch, tmp_path):
    target = tmp_path / "custom-config"
    monkeypatch.setenv("LUNCHMENU_CONFIG_DIR", str(target))
    resolved = config.config_dir()
    assert resolved == target
    assert target.is_dir()
    assert (target.stat().st_mode & 0o777) == 0o700


def test_config_dir_defaults_under_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.delenv("LUNCHMENU_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    resolved = config.config_dir()
    assert resolved == tmp_path / "xdg" / "lunchmenu"


def test_tvs_and_keys_paths_resolve_under_config_dir(monkeypatch, tmp_path):
    """webos.py's KEYS_PATH/TVS_PATH became lazy accessors -- confirm they
    resolve under the config dir, not the state dir."""
    from lunchmenu import webos

    target = tmp_path / "custom-config"
    monkeypatch.setenv("LUNCHMENU_CONFIG_DIR", str(target))
    assert webos.tvs_path() == target / "tvs.json"
    assert webos.keys_path() == target / "webos-keys.json"


def test_migration_moves_tvs_and_keys_from_state_to_config_dir_preserving_mode(
    monkeypatch, tmp_path
):
    """Anyone upgrading from the state-dir-only layout: tvs.json and
    webos-keys.json must be moved (not copied) into the config dir the
    first time anything asks for either directory, and webos-keys.json must
    keep mode 600 in its new home."""
    # Named distinctly from the autouse fixture's own tmp_path/"state" and
    # tmp_path/"config" (which already exist) to avoid a mkdir collision.
    state = tmp_path / "migration-state"
    state.mkdir()
    conf = tmp_path / "migration-config"
    conf.mkdir()

    (state / "tvs.json").write_text('{"1.2.3.4": {"room": "Kitchen"}}\n')
    (state / "webos-keys.json").write_text('{"1.2.3.4": "secret-key"}')
    (state / "webos-keys.json").chmod(0o600)

    monkeypatch.setenv("LUNCHMENU_STATE_DIR", str(state))
    monkeypatch.setenv("LUNCHMENU_CONFIG_DIR", str(conf))
    # This suite's autouse fixture already ran the migration once (finding
    # nothing to move); force it to run again against this test's files.
    monkeypatch.setattr(config, "_migrated", False)

    config.state_dir()
    config.config_dir()

    assert not (state / "tvs.json").exists()
    assert not (state / "webos-keys.json").exists()
    assert (conf / "tvs.json").read_text() == '{"1.2.3.4": {"room": "Kitchen"}}\n'
    assert (conf / "webos-keys.json").read_text() == '{"1.2.3.4": "secret-key"}'
    assert ((conf / "webos-keys.json").stat().st_mode & 0o777) == 0o600


def test_migration_moves_repo_root_config_toml_to_config_dir(monkeypatch, tmp_path):
    """A config.toml left at the repo root (the original layout, before
    ~/.config/lunchmenu/config.toml existed) must be moved into the config
    dir on first use -- not copied, not silently ignored. Uses a fake
    "repo root" (via config._legacy_repo_dir) rather than this actual
    checkout."""
    fake_repo_root = tmp_path / "fake-repo"
    fake_repo_root.mkdir()
    (fake_repo_root / "config.toml").write_text('[school]\nidentifier = "from-repo-root"\n')
    # Named distinctly from the autouse fixture's own tmp_path/"config"
    # (which already exists) to avoid a mkdir collision.
    conf = tmp_path / "migration-config"
    conf.mkdir()

    monkeypatch.setattr(config, "_legacy_repo_dir", lambda: fake_repo_root)
    monkeypatch.setenv("LUNCHMENU_CONFIG_DIR", str(conf))
    monkeypatch.setattr(config, "_migrated", False)

    config.config_dir()

    assert not (fake_repo_root / "config.toml").exists()
    assert (conf / "config.toml").read_text() == '[school]\nidentifier = "from-repo-root"\n'
