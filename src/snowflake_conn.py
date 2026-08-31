"""Snowflake connection helper.

Credentials are read from environment variables and from nowhere else. There
is no config file, no keyring lookup, no interactive prompt and no default
value for any of them. That is a deliberate constraint, not an oversight: a
credential that can be supplied by a file is a credential that can be
committed by accident, and a tool that asks for a password interactively
teaches people to paste production secrets into terminals and chat windows.
Environment variables keep the secret in the operator's shell where it
belongs, and .gitignore blocks the usual leak paths.

Nothing in this module ever logs, prints or echoes a credential VALUE - only
variable NAMES appear in messages.

--------------------------------------------------------------------------
TWO AUTHENTICATION MODES
--------------------------------------------------------------------------
Password auth is the default because it is the shortest path to a first run.
It is also the mode most likely to stop working: Snowflake has been phasing out
password-only sign-in for human users in favour of enforced MFA, and a
scheduled reconciliation job cannot answer an MFA prompt at 3am.

So the module also supports KEY-PAIR authentication, which is how automated
workloads are meant to connect. Setting SNOWFLAKE_PRIVATE_KEY_FILE switches
modes; SNOWFLAKE_PASSWORD is then neither required nor read. The mode is
inferred from which variables are present rather than from a separate
SNOWFLAKE_AUTH_MODE flag, because a flag that disagrees with the variables
actually set is one more thing to get wrong at 3am.

Key-pair is the better answer to "how would you authenticate this in
production", and the reason is worth stating: a private key can be rotated,
scoped to a service user, and stored in a secrets manager without a human ever
seeing it - none of which is true of a password in a shell variable.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Needed whichever way you authenticate.
COMMON_ENV_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
)

PASSWORD_ENV_VAR = "SNOWFLAKE_PASSWORD"
PRIVATE_KEY_ENV_VAR = "SNOWFLAKE_PRIVATE_KEY_FILE"
PRIVATE_KEY_PASSPHRASE_ENV_VAR = "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"

AUTH_PASSWORD = "password"
AUTH_KEY_PAIR = "key_pair"

# The password-mode requirement set. Kept under this name because password is
# the default mode; required_env_vars() below is the mode-aware version and is
# what the checks actually use.
REQUIRED_ENV_VARS = COMMON_ENV_VARS + (PASSWORD_ENV_VAR,)

# Optional. SNOWFLAKE_ROLE absent means Snowflake uses the user's default role;
# the passphrase is only needed when the private key file is encrypted.
OPTIONAL_ENV_VARS = ("SNOWFLAKE_ROLE", PRIVATE_KEY_PASSPHRASE_ENV_VAR)


def auth_mode() -> str:
    """Which authentication mode the current environment selects.

    Presence of SNOWFLAKE_PRIVATE_KEY_FILE means key-pair; otherwise password.
    Inferring from the variables rather than a separate mode flag means the
    two can never contradict each other.
    """
    if os.environ.get(PRIVATE_KEY_ENV_VAR, "").strip():
        return AUTH_KEY_PAIR
    return AUTH_PASSWORD


def required_env_vars() -> tuple[str, ...]:
    """The variables this environment actually needs, given its auth mode."""
    if auth_mode() == AUTH_KEY_PAIR:
        return COMMON_ENV_VARS + (PRIVATE_KEY_ENV_VAR,)
    return COMMON_ENV_VARS + (PASSWORD_ENV_VAR,)


class SnowflakeConfigError(RuntimeError):
    """Raised when Snowflake configuration is incomplete.

    A distinct exception type so callers can catch a configuration problem and
    print the actionable message, while letting genuine connection or SQL
    errors propagate with their full traceback. Those are different problems
    and deserve different presentation.
    """


def missing_env_vars() -> list[str]:
    """Names of required variables that are unset or blank, in declared order.

    Blank counts as missing. `export SNOWFLAKE_PASSWORD=` is far more likely to
    be a typo or a failed secret lookup than a genuine empty password, and
    treating it as set produces an authentication error that sends the operator
    hunting through Snowflake's access logs instead of at their own shell.
    """
    return [name for name in required_env_vars() if not os.environ.get(name, "").strip()]


def load_private_key() -> bytes:
    """Read the private key file and return it as DER bytes for the connector.

    The connector will accept a file path directly, but parsing the key here
    buys something worth the extra code: every way this can fail gets an error
    that names the actual problem. Handing the path straight to the driver
    turns "your key is encrypted and you did not set a passphrase" into a
    generic authentication failure, and the operator then goes looking for the
    fault in Snowflake rather than on their own disk.

    The key material never leaves this function except as the return value, and
    no branch below includes key or passphrase content in a message.
    """
    from cryptography.hazmat.primitives import serialization

    raw_path = os.environ.get(PRIVATE_KEY_ENV_VAR, "").strip()
    path = Path(raw_path).expanduser()

    if not path.exists():
        raise SnowflakeConfigError(
            f"{PRIVATE_KEY_ENV_VAR} points to a file that does not exist:\n"
            f"  {path}\n\n"
            "Set it to the path of your PEM-encoded private key "
            "(commonly rsa_key.p8)."
        )
    if path.is_dir():
        raise SnowflakeConfigError(
            f"{PRIVATE_KEY_ENV_VAR} points to a directory, not a file:\n  {path}"
        )

    try:
        key_bytes = path.read_bytes()
    except OSError as exc:
        raise SnowflakeConfigError(
            f"Cannot read the private key file at {path}: {exc.strerror}"
        ) from exc

    # A private key readable by anyone on the machine is a finding in its own
    # right. Warned rather than refused: the key may live on a single-user
    # laptop, and hard-failing here would push people toward workarounds worse
    # than the warning. The check is skipped on Windows, where POSIX mode bits
    # do not mean what they appear to.
    if os.name == "posix":
        mode = path.stat().st_mode
        if mode & 0o077:
            print(
                f"WARNING: {path} is readable by group or other "
                f"(mode {mode & 0o777:03o}). Private keys should be 0600:\n"
                f"  chmod 600 {path}",
                file=sys.stderr,
            )

    passphrase = os.environ.get(PRIVATE_KEY_PASSPHRASE_ENV_VAR, "")
    passphrase_bytes = passphrase.encode() if passphrase else None

    try:
        private_key = serialization.load_pem_private_key(
            key_bytes, password=passphrase_bytes
        )
    except TypeError as exc:
        # cryptography raises TypeError for the encrypted/passphrase mismatch
        # cases, and its own message is too terse to act on.
        if passphrase_bytes is None:
            raise SnowflakeConfigError(
                f"The private key at {path} is encrypted, but "
                f"{PRIVATE_KEY_PASSPHRASE_ENV_VAR} is not set.\n\n"
                f"  export {PRIVATE_KEY_PASSPHRASE_ENV_VAR}=...\n\n"
                "Or generate an unencrypted key if this is a service account "
                "whose key file is already protected at rest."
            ) from exc
        raise SnowflakeConfigError(
            f"The private key at {path} is not encrypted, but "
            f"{PRIVATE_KEY_PASSPHRASE_ENV_VAR} is set. Unset it and re-run."
        ) from exc
    except ValueError as exc:
        raise SnowflakeConfigError(
            f"Could not load the private key at {path}.\n\n"
            "Most likely one of:\n"
            "  - the passphrase is wrong\n"
            "  - the file is a PUBLIC key, not a private one\n"
            "  - the file is not PEM-encoded\n\n"
            "A private key file starts with '-----BEGIN PRIVATE KEY-----' or "
            "'-----BEGIN ENCRYPTED PRIVATE KEY-----'."
        ) from exc

    # Snowflake wants the key as unencrypted DER (PKCS#8). This is an in-memory
    # re-encoding only - nothing unencrypted is ever written to disk.
    return private_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def describe_config() -> str:
    """A human-readable summary with the secret values redacted.

    Printed at the top of every Snowflake run so the operator can confirm WHICH
    account and database they just pointed a regulatory tie-out at, before it
    writes anything. Account, user, warehouse, database and schema are shown
    because getting those wrong is the actual risk. The password is only ever
    reported as set or not set.
    """
    mode = auth_mode()
    lines = [f"  {'auth mode':<30} {mode}"]

    for name in required_env_vars():
        value = os.environ.get(name, "")
        if name == PASSWORD_ENV_VAR:
            # Never the value, only whether one is present.
            lines.append(f"  {name:<30} {'set' if value.strip() else 'NOT SET'}")
        else:
            # The private key FILE PATH is shown: it is not secret, and a
            # wrong path is one of the likelier things to get wrong.
            lines.append(f"  {name:<30} {value.strip() or 'NOT SET'}")

    role = os.environ.get("SNOWFLAKE_ROLE", "").strip()
    lines.append(f"  {'SNOWFLAKE_ROLE':<30} {role or '(not set - using default role)'}")

    if mode == AUTH_KEY_PAIR:
        has_passphrase = bool(os.environ.get(PRIVATE_KEY_PASSPHRASE_ENV_VAR, "").strip())
        lines.append(
            f"  {PRIVATE_KEY_PASSPHRASE_ENV_VAR:<30} "
            f"{'set' if has_passphrase else '(not set - key must be unencrypted)'}"
        )

    return "\n".join(lines)


def check_env() -> None:
    """Raise a SnowflakeConfigError naming exactly what is missing.

    The whole point of this function is that the operator should never see a
    raw connector traceback for a problem that is entirely on their side of the
    boundary. A stack trace ending in `250001: Could not connect to Snowflake
    backend` tells them nothing about which of six variables they forgot; this
    tells them precisely, and gives them the export lines to fix it.
    """
    missing = missing_env_vars()
    if not missing:
        return

    required = required_env_vars()
    mode = auth_mode()

    # Name the mode in the error. Without it, someone who set the key file but
    # not the user sees SNOWFLAKE_PASSWORD absent from the list and reasonably
    # wonders whether the tool has noticed their key at all.
    mode_note = (
        f"Authenticating with a key pair ({PRIVATE_KEY_ENV_VAR} is set), "
        f"so {PASSWORD_ENV_VAR} is not required.\n"
        if mode == AUTH_KEY_PAIR
        else
        f"Authenticating with a password. To use a key pair instead - which is "
        f"what an automated job should do, and what works when the account "
        f"enforces MFA - set {PRIVATE_KEY_ENV_VAR} to your private key path.\n"
    )

    raise SnowflakeConfigError(
        "Snowflake configuration is incomplete.\n\n"
        + mode_note
        + f"\nMissing or blank ({len(missing)} of {len(required)} required):\n"
        + "".join(f"  - {name}\n" for name in missing)
        + "\nSet them in your shell and re-run, for example:\n"
        + "".join(f"  export {name}=...\n" for name in missing)
        + "\nThese are read from the environment only - never pass a credential "
          "on the command line, paste one into a chat, or write one to a file "
          "in this repository.\n"
        + "\nTo run against local SQLite instead, leave USE_SNOWFLAKE unset."
    )


def connect():
    """Open a Snowflake connection from the environment.

    Imported lazily so that the entire SQLite path works on a machine where
    snowflake-connector-python is not installed. Importing at module scope
    would make an optional backend a hard dependency of the local dev loop.
    """
    check_env()

    try:
        import snowflake.connector
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SnowflakeConfigError(
            "snowflake-connector-python is not installed.\n"
            "  pip install -r requirements.txt"
        ) from exc

    kwargs = {
        "account":   os.environ["SNOWFLAKE_ACCOUNT"].strip(),
        "user":      os.environ["SNOWFLAKE_USER"].strip(),
        "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"].strip(),
        "database":  os.environ["SNOWFLAKE_DATABASE"].strip(),
        "schema":    os.environ["SNOWFLAKE_SCHEMA"].strip(),
    }

    if auth_mode() == AUTH_KEY_PAIR:
        # DER bytes rather than handing the connector the file path: parsing it
        # in load_private_key() is what lets a bad key produce an error naming
        # the actual fault instead of a generic authentication failure.
        kwargs["private_key"] = load_private_key()
    else:
        kwargs["password"] = os.environ[PASSWORD_ENV_VAR]

    role = os.environ.get("SNOWFLAKE_ROLE", "").strip()
    if role:
        kwargs["role"] = role

    return snowflake.connector.connect(**kwargs)


@contextmanager
def connection() -> Iterator["object"]:
    """Context-managed connection that always closes.

    A leaked connection holds a warehouse session open, and an idle warehouse
    that never suspends is a line on somebody's bill. Closing in a finally
    block means that stays true even when a query raises.
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def cli_main(entrypoint) -> int:
    """Run a Snowflake CLI entrypoint, turning config errors into plain output.

    A missing environment variable is an operator problem with a known remedy,
    not a defect in this code, and printing 12 frames of Python internals above
    the one line that matters actively buries the answer. SnowflakeConfigError
    already carries a message naming exactly which variables are unset and how
    to set them, so it is printed on its own and the process exits 1.

    Only SnowflakeConfigError is swallowed. A genuine connection failure, a SQL
    error or a row-count mismatch still raises with its full traceback, because
    for those the traceback IS the useful information.
    """
    try:
        entrypoint()
    except SnowflakeConfigError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    return 0
