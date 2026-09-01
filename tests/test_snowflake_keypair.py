"""Tests for key-pair authentication.

None of these need a Snowflake account. Everything they check happens before a
socket is opened: which mode the environment selects, which variables that mode
requires, and whether a bad key file produces an error naming the actual fault.

That last property is the reason the key is parsed here rather than handed to
the connector as a path. A generic authentication failure sends the operator
looking for the problem in Snowflake, when it is on their own disk.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.snowflake_conn import (  # noqa: E402
    AUTH_KEY_PAIR,
    AUTH_PASSWORD,
    COMMON_ENV_VARS,
    PASSWORD_ENV_VAR,
    PRIVATE_KEY_ENV_VAR,
    PRIVATE_KEY_PASSPHRASE_ENV_VAR,
    SnowflakeConfigError,
    auth_mode,
    check_env,
    describe_config,
    load_private_key,
    missing_env_vars,
    required_env_vars,
    set_env_instructions,
)
from src import snowflake_conn  # noqa: E402

PASSPHRASE = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def clean_snowflake_env(monkeypatch):
    """Every test starts from a known-empty Snowflake environment.

    Without this, a variable set by the developer's shell - or left behind by
    another test - would silently flip the auth mode and make these assertions
    about something other than what they claim.
    """
    for name in (
        *COMMON_ENV_VARS, PASSWORD_ENV_VAR,
        PRIVATE_KEY_ENV_VAR, PRIVATE_KEY_PASSPHRASE_ENV_VAR, "SNOWFLAKE_ROLE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def keys(tmp_path_factory) -> dict[str, Path]:
    """A real RSA key pair, written out unencrypted, encrypted, and public."""
    directory = tmp_path_factory.mktemp("keys")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    plain = directory / "rsa_key.p8"
    plain.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    plain.chmod(0o600)

    encrypted = directory / "rsa_key_encrypted.p8"
    encrypted.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(PASSPHRASE.encode()),
    ))
    encrypted.chmod(0o600)

    public = directory / "rsa_key.pub"
    public.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    public.chmod(0o600)

    return {"plain": plain, "encrypted": encrypted, "public": public}


# ===========================================================================
# Mode selection
# ===========================================================================
def test_password_is_the_default_mode():
    assert auth_mode() == AUTH_PASSWORD
    assert PASSWORD_ENV_VAR in required_env_vars()
    assert PRIVATE_KEY_ENV_VAR not in required_env_vars()


def test_setting_a_key_file_switches_to_key_pair(monkeypatch, keys):
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["plain"]))
    assert auth_mode() == AUTH_KEY_PAIR


def test_key_pair_mode_does_not_require_a_password(monkeypatch, keys):
    """The point of the mode: no password anywhere.

    An account with MFA enforced cannot use one, and a scheduled job could not
    answer the prompt even if it could. Continuing to demand the variable would
    make the whole mode pointless.
    """
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["plain"]))
    for name in COMMON_ENV_VARS:
        monkeypatch.setenv(name, "value")

    assert PASSWORD_ENV_VAR not in required_env_vars()
    assert missing_env_vars() == []
    check_env()  # must not raise


def test_key_pair_mode_still_requires_the_common_variables(monkeypatch, keys):
    """Switching auth mode must not accidentally waive the rest."""
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["plain"]))
    assert missing_env_vars() == list(COMMON_ENV_VARS)


def test_blank_key_file_path_falls_back_to_password(monkeypatch):
    """`export SNOWFLAKE_PRIVATE_KEY_FILE=` must not select key-pair mode.

    Selecting a mode on a blank value would demand a key that was never
    configured, and the resulting error would point at the key rather than at
    the empty assignment that caused it.
    """
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, "   ")
    assert auth_mode() == AUTH_PASSWORD
    assert PASSWORD_ENV_VAR in required_env_vars()


def test_error_message_names_the_active_mode(monkeypatch, keys):
    """Someone who set the key file but forgot the user should not see
    SNOWFLAKE_PASSWORD absent from the list and wonder whether the tool noticed
    their key at all."""
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["plain"]))

    with pytest.raises(SnowflakeConfigError) as exc:
        check_env()
    assert "key pair" in str(exc.value).lower()
    assert PASSWORD_ENV_VAR in str(exc.value), (
        "the message should say the password is NOT required, so mentioning it "
        "by name is expected here"
    )


def test_password_mode_error_points_toward_key_pair():
    """The password-mode message should mention the alternative, because MFA
    enforcement is the likeliest reason someone is reading that error."""
    with pytest.raises(SnowflakeConfigError) as exc:
        check_env()
    message = str(exc.value)
    assert PRIVATE_KEY_ENV_VAR in message
    assert "MFA" in message


# ===========================================================================
# Loading the key
# ===========================================================================
def test_unencrypted_key_loads(monkeypatch, keys):
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["plain"]))
    der = load_private_key()
    assert isinstance(der, bytes) and len(der) > 100


def test_encrypted_key_loads_with_its_passphrase(monkeypatch, keys):
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["encrypted"]))
    monkeypatch.setenv(PRIVATE_KEY_PASSPHRASE_ENV_VAR, PASSPHRASE)
    assert len(load_private_key()) > 100


def test_both_key_files_yield_identical_der(monkeypatch, keys):
    """Encrypted and unencrypted files hold the SAME key, so the DER handed to
    the connector must be byte-identical. A mismatch would mean the decryption
    path silently produced a different key - which would authenticate as
    nobody, with an error blaming Snowflake."""
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["plain"]))
    from_plain = load_private_key()

    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["encrypted"]))
    monkeypatch.setenv(PRIVATE_KEY_PASSPHRASE_ENV_VAR, PASSPHRASE)
    from_encrypted = load_private_key()

    assert from_plain == from_encrypted


def test_encrypted_key_without_passphrase_says_exactly_that(monkeypatch, keys):
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["encrypted"]))

    with pytest.raises(SnowflakeConfigError) as exc:
        load_private_key()
    message = str(exc.value)
    assert "encrypted" in message
    assert PRIVATE_KEY_PASSPHRASE_ENV_VAR in message


def test_wrong_passphrase_is_reported_as_a_key_problem(monkeypatch, keys):
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["encrypted"]))
    monkeypatch.setenv(PRIVATE_KEY_PASSPHRASE_ENV_VAR, "not-the-passphrase")

    with pytest.raises(SnowflakeConfigError) as exc:
        load_private_key()
    assert "passphrase is wrong" in str(exc.value)


def test_passphrase_set_on_an_unencrypted_key_is_explained(monkeypatch, keys):
    """A confusing combination that produces a confusing driver error if
    passed through untouched."""
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["plain"]))
    monkeypatch.setenv(PRIVATE_KEY_PASSPHRASE_ENV_VAR, PASSPHRASE)

    with pytest.raises(SnowflakeConfigError) as exc:
        load_private_key()
    assert "not encrypted" in str(exc.value)


def test_public_key_by_mistake_is_caught(monkeypatch, keys):
    """Registering the public key with Snowflake and then pointing the client
    at it too is an easy and very confusing mistake to make."""
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["public"]))

    with pytest.raises(SnowflakeConfigError) as exc:
        load_private_key()
    assert "PUBLIC key" in str(exc.value)


def test_missing_file_names_the_path(monkeypatch, tmp_path):
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(tmp_path / "absent.p8"))

    with pytest.raises(SnowflakeConfigError) as exc:
        load_private_key()
    assert "does not exist" in str(exc.value)
    assert "absent.p8" in str(exc.value)


def test_directory_instead_of_file(monkeypatch, tmp_path):
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(tmp_path))
    with pytest.raises(SnowflakeConfigError) as exc:
        load_private_key()
    assert "directory" in str(exc.value)


def test_tilde_in_the_path_is_expanded(monkeypatch, keys):
    """~/rsa_key.p8 is how people write this path, and os.environ does not
    expand it the way a shell would."""
    monkeypatch.setenv("HOME", str(keys["plain"].parent))
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, f"~/{keys['plain'].name}")
    assert len(load_private_key()) > 100


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
def test_loose_permissions_warn_but_do_not_block(monkeypatch, tmp_path, keys, capsys):
    """A world-readable private key is a real finding, but refusing to run
    would push people toward workarounds worse than the warning."""
    loose = tmp_path / "loose.p8"
    loose.write_bytes(keys["plain"].read_bytes())
    loose.chmod(0o644)

    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(loose))
    der = load_private_key()

    assert len(der) > 100, "a loose-permission key must still load"
    assert "readable by group or other" in capsys.readouterr().err

    # And a correctly-permissioned key must stay silent, or the warning becomes
    # noise everyone learns to ignore.
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["plain"]))
    assert stat.S_IMODE(keys["plain"].stat().st_mode) == 0o600
    load_private_key()
    assert "readable by group" not in capsys.readouterr().err


# ===========================================================================
# Nothing secret is ever printed
# ===========================================================================
def test_config_summary_shows_the_key_path_but_not_the_passphrase(monkeypatch, keys):
    """The path is not secret and a wrong one is a likely mistake, so it is
    shown. The passphrase is secret, so only its presence is."""
    for name in COMMON_ENV_VARS:
        monkeypatch.setenv(name, f"value-for-{name}")
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["encrypted"]))
    monkeypatch.setenv(PRIVATE_KEY_PASSPHRASE_ENV_VAR, PASSPHRASE)

    summary = describe_config()

    assert PASSPHRASE not in summary
    assert str(keys["encrypted"]) in summary
    assert "key_pair" in summary


def test_config_summary_never_contains_key_material(monkeypatch, keys):
    for name in COMMON_ENV_VARS:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["plain"]))

    summary = describe_config()
    assert "BEGIN PRIVATE KEY" not in summary
    assert "MII" not in summary, "base64 key material leaked into the summary"


# ===========================================================================
# The remedy must be typable in the shell the operator is actually using
# ===========================================================================
def test_posix_instructions_use_export(monkeypatch):
    monkeypatch.setattr(snowflake_conn, "_is_windows", lambda: False)
    text = set_env_instructions(["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER"])

    assert "export SNOWFLAKE_ACCOUNT=..." in text
    assert "$env:" not in text
    assert "set SNOWFLAKE_ACCOUNT=" not in text


def test_windows_instructions_never_say_export(monkeypatch):
    """The bug this test exists for: a Windows user was told to run `export`.

    An error message whose remedy cannot be typed is not a helpful error - it
    is a second error, arriving exactly when someone is already stuck. On
    Windows the output must be PowerShell and cmd syntax, and `export` must not
    appear at all.
    """
    monkeypatch.setattr(snowflake_conn, "_is_windows", lambda: True)
    text = set_env_instructions(["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_PASSWORD"])

    assert "export " not in text, "Windows users were told to run a bash builtin"
    assert '$env:SNOWFLAKE_ACCOUNT="..."' in text
    assert "set SNOWFLAKE_ACCOUNT=..." in text


def test_windows_instructions_warn_that_variables_are_session_scoped(monkeypatch):
    """"I set it and it still says missing" is the predictable next question."""
    monkeypatch.setattr(snowflake_conn, "_is_windows", lambda: True)
    text = set_env_instructions(["SNOWFLAKE_ACCOUNT"])
    assert "current terminal window" in text
    assert "Environment Variables" in text


@pytest.mark.parametrize("on_windows", [False, True])
def test_check_env_message_matches_the_platform(monkeypatch, on_windows):
    """End to end: the real error a user sees, on both platforms."""
    monkeypatch.setattr(snowflake_conn, "_is_windows", lambda: on_windows)

    with pytest.raises(SnowflakeConfigError) as exc:
        check_env()
    message = str(exc.value)

    if on_windows:
        assert "export " not in message
        assert "$env:" in message
    else:
        assert "export " in message
        assert "$env:" not in message


def test_encrypted_key_message_matches_the_platform(monkeypatch, keys):
    """The passphrase hint had the same bug as the missing-variable message."""
    monkeypatch.setenv(PRIVATE_KEY_ENV_VAR, str(keys["encrypted"]))
    monkeypatch.setattr(snowflake_conn, "_is_windows", lambda: True)

    with pytest.raises(SnowflakeConfigError) as exc:
        load_private_key()
    message = str(exc.value)

    assert "export " not in message
    assert f'$env:{PRIVATE_KEY_PASSPHRASE_ENV_VAR}="..."' in message
