from voice.ice import describe_ice_env, parse_ice_servers


def test_no_value_means_host_candidates_only():
    status = parse_ice_servers(None)
    assert status.configured is False
    assert status.ok_for_public_call is False
    assert "host candidates only" in status.summary()

    assert parse_ice_servers("   ").configured is False


def test_single_bare_stun_url():
    status = parse_ice_servers("stun:stun.l.google.com:19302")
    assert status.has_stun is True
    assert status.has_turn is False
    assert status.ok_for_public_call is True
    assert "STUN only" in status.summary()


def test_comma_separated_stun_and_turn():
    status = parse_ice_servers(
        "stun:stun.l.google.com:19302,turn:turn.example.com:3478"
    )
    assert status.has_stun is True
    assert status.has_turn is True
    assert status.ok_for_public_call is True
    assert "STUN and TURN" in status.summary()


def test_turn_alone_also_counts_as_stun_capable():
    status = parse_ice_servers("turn:turn.example.com:3478")
    assert status.has_turn is True
    assert status.has_stun is True


def test_json_array_with_turn_credentials():
    raw = (
        '[{"urls": "stun:stun.l.google.com:19302"}, '
        '{"urls": "turn:turn.example.com:3478", "username": "u", "credential": "p"}]'
    )
    status = parse_ice_servers(raw)
    assert status.has_stun is True
    assert status.has_turn is True
    assert status.problems == []


def test_json_entry_without_urls_is_a_problem():
    status = parse_ice_servers('[{"username": "u", "credential": "p"}]')
    assert status.problems
    assert status.ok_for_public_call is False


def test_json_entry_with_unexpected_key_is_flagged():
    status = parse_ice_servers('[{"urls": "turn:t:3478", "password": "p"}]')
    assert any("unexpected key" in problem for problem in status.problems)


def test_malformed_json_is_reported_not_raised():
    status = parse_ice_servers("[not json")
    assert status.problems
    assert "invalid" in status.summary().lower()


def test_unknown_scheme_is_flagged():
    status = parse_ice_servers("http://not-an-ice-server")
    assert any("scheme" in problem for problem in status.problems)


def test_describe_ice_env_never_raises():
    # Even a value that would break json.loads deep inside returns a status.
    assert describe_ice_env("[{]").problems
    assert describe_ice_env(None).configured is False


def test_summary_never_contains_turn_credentials():
    # A credential leak in logs is the risk the brief calls out explicitly.
    raw = '[{"urls": "turn:turn.example.com:3478", "username": "secret-user", "credential": "secret-pass"}]'
    status = parse_ice_servers(raw)
    text = status.summary()
    assert "secret-user" not in text
    assert "secret-pass" not in text
    assert "turn.example.com" not in text   # the URL itself is not echoed either


def test_production_requires_stun_and_origins():
    from voice.ice import production_config_errors

    # development: no requirements, even with nothing configured
    assert production_config_errors(
        app_env="development", ice=parse_ice_servers(None), allowed_origins=None
    ) == []

    # production, nothing set: both errors, and no credential text
    errors = production_config_errors(
        app_env="production", ice=parse_ice_servers(None), allowed_origins=None
    )
    assert any("STUN" in e for e in errors)
    assert any("PIPECAT_ALLOWED_ORIGINS" in e for e in errors)


def test_production_passes_with_stun_and_origins():
    from voice.ice import production_config_errors

    ok = production_config_errors(
        app_env="production",
        ice=parse_ice_servers("stun:stun.l.google.com:19302"),
        allowed_origins="https://demo.example.com",
    )
    assert ok == []


def test_production_rejects_malformed_ice_without_leaking():
    from voice.ice import production_config_errors

    raw = '[{"urls": "turn:t:3478", "username": "u", "credential": "p", "bad": 1}]'
    errors = production_config_errors(
        app_env="production",
        ice=parse_ice_servers(raw),
        allowed_origins="https://demo.example.com",
    )
    assert errors and "invalid" in errors[0].lower()
    assert "u" not in " ".join(errors).split()   # no bare credential token
    assert "p" not in " ".join(errors).split()
