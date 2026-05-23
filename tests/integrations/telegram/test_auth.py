from clk_harness.integrations.telegram.auth import (
    _parse_ids,
    is_allowed,
    load_allowlist,
)


def test_parse_ids_basic():
    assert _parse_ids("1,2,3") == [1, 2, 3]
    assert _parse_ids("") == []
    assert _parse_ids(None) == []


def test_parse_ids_drops_garbage():
    assert _parse_ids("1, abc, 3, , 4x") == [1, 3]


def test_load_allowlist_env_precedence():
    env = {"CLK_TELEGRAM_ALLOWED_USERS": "10,20"}
    out = load_allowlist(config_ids=[30], extra_ids=[40], env=env)
    assert out == {10, 20, 30, 40}


def test_load_allowlist_empty():
    assert load_allowlist(env={}) == set()


def test_load_allowlist_coerces_strings():
    assert load_allowlist(config_ids=["1", "2", "bad"], env={}) == {1, 2}


def test_is_allowed():
    al = {1, 2}
    assert is_allowed(1, al)
    assert not is_allowed(3, al)
    assert not is_allowed(None, al)
