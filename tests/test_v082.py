"""0.8.2: `dev --help` and `dev grant --help` list the grants this host can give."""
from orchard import cli
from orchard.config import Config


def _cfg() -> Config:
    cfg = Config()
    cfg.github_app_id, cfg.github_key_file, cfg.github_owner = "1", "/k", "spott"
    cfg.ssh_ca_key_file = "/ca"
    cfg.ssh_principals = {"ro": "read-only login on every host", "deploy": "ops on every host"}
    cfg.deploy_token_file = "/t"
    cfg.k8s_server, cfg.k8s_ca_file, cfg.k8s_token_file = "https://k", "/ca.crt", "/tok"
    cfg.k8s_roles = {
        "mcp": {"namespace": "mcp", "serviceAccount": "orchard-view", "description": "read-only"},
        "bare": {"namespace": "x", "serviceAccount": "sa"},
    }
    return cfg


def test_catalog_lists_every_grant_with_its_meaning():
    text = cli.grants_help(_cfg())
    assert "github:<repo>" in text and "under spott" in text and "contents=write" in text
    assert "ssh:ro" in text and "read-only login on every host" in text
    assert "ops on every host; opens the deploy window; needs --for, at most 4h" in text
    assert "k8s:mcp" in text and "read-only (namespace mcp)" in text
    assert "ServiceAccount sa (namespace x)" in text


def test_catalog_says_what_is_off():
    text = cli.grants_help(Config())
    assert text.count("off on this host") == 3


def test_catalog_without_a_config_names_the_kinds():
    text = cli.grants_help(None)
    assert "github:<repo>" in text and "ssh:<principal>" in text and "k8s:<role>" in text


def test_help_carries_the_catalog(capsys):
    for argv in (["--help"], ["grant", "--help"]):
        try:
            cli.build_parser(_cfg()).parse_args(argv)
        except SystemExit:
            pass
        out = capsys.readouterr().out
        assert "k8s:mcp" in out and "ssh:ro" in out


# -- 0.8.3: `dev grants`, also from inside a workspace -----------------------

def test_grants_verb_shows_holdings_and_the_command_to_ask_for(mgr, capsys):
    mgr.cfg.k8s_server, mgr.cfg.k8s_ca_file, mgr.cfg.k8s_token_file = "https://k", "/ca.crt", "/tok"
    mgr.cfg.k8s_roles = {"mcp": {"namespace": "mcp", "serviceAccount": "sa"}}
    mgr.create("foo")
    mgr.grant("foo", "ssh", "ro")
    args = cli.build_parser(mgr.cfg).parse_args(["grants", "foo"])
    assert args.fn(mgr, args) == 0
    out = capsys.readouterr().out
    assert "foo holds:" in out and "ssh:ro" in out and "standing" in out
    assert "dev grant foo <kind>:<value>" in out
    assert out.count("dev grant foo ") == 2 and "dev grants foo shows" in out  # header too, with roles configured
    assert "cannot grant itself" in out


def test_grants_verb_is_self_service_and_only_for_itself():
    import pytest
    assert cli.self_argv(["grants"], "foo") == ["grants", "foo"]
    with pytest.raises(cli.SelfRefused):
        cli.self_argv(["grants", "bar"], "foo")
    with pytest.raises(cli.SelfRefused):
        cli.self_argv(["grant", "foo", "ssh:deploy"], "foo")
