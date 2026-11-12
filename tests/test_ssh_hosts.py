"""Host aliases are read from an OpenSSH client config."""

from phidler.ssh_hosts import list_ssh_hosts


def test_lists_concrete_hosts_in_order_without_duplicates(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text(
        "# a comment\n"
        "Host gpubox\n"
        "    HostName 10.0.0.5\n"
        "host cluster login.cluster.edu\n"   # keyword case, several names
        "Host *\n"
        "    ForwardAgent no\n"
        "Host *.lab !bad.lab node?\n"        # patterns only
        "Host=equals\n"
        "Host = spaced\n"
        'Host "quoted"\n'
        "Host gpubox\n"                      # repeated block
        "Match host other\n"
        "    User me\n"
    )
    assert list_ssh_hosts(cfg) == [
        "gpubox", "cluster", "login.cluster.edu", "equals", "spaced", "quoted",
    ]


def test_follows_includes_relative_to_the_config_directory(tmp_path):
    (tmp_path / "conf.d").mkdir()
    (tmp_path / "conf.d" / "b.conf").write_text("Host beta\n")
    (tmp_path / "conf.d" / "a.conf").write_text("Host alpha\n")
    other = tmp_path / "elsewhere"
    other.write_text("Host absolute\n")
    (tmp_path / "config").write_text(
        f"Include conf.d/*.conf {other}\nInclude missing/*\nHost last\n"
    )
    assert list_ssh_hosts(tmp_path / "config") == ["alpha", "beta", "absolute", "last"]


def test_include_cycle_terminates(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Host one\nInclude config\n")
    assert list_ssh_hosts(cfg) == ["one"]


def test_missing_config_gives_no_hosts(tmp_path):
    assert list_ssh_hosts(tmp_path / "nope") == []
