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
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

REQUIRED_ENV_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
)

# Optional. Absent means Snowflake uses the user's default role.
OPTIONAL_ENV_VARS = ("SNOWFLAKE_ROLE",)


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
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name, "").strip()]


def describe_config() -> str:
    """A human-readable summary with the secret values redacted.

    Printed at the top of every Snowflake run so the operator can confirm WHICH
    account and database they just pointed a regulatory tie-out at, before it
    writes anything. Account, user, warehouse, database and schema are shown
    because getting those wrong is the actual risk. The password is only ever
    reported as set or not set.
    """
    lines = []
    for name in REQUIRED_ENV_VARS:
        value = os.environ.get(name, "")
        if name == "SNOWFLAKE_PASSWORD":
            lines.append(f"  {name:<22} {'set' if value.strip() else 'NOT SET'}")
        else:
            lines.append(f"  {name:<22} {value.strip() or 'NOT SET'}")
    for name in OPTIONAL_ENV_VARS:
        value = os.environ.get(name, "").strip()
        lines.append(f"  {name:<22} {value or '(not set - using default role)'}")
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

    raise SnowflakeConfigError(
        "Snowflake configuration is incomplete.\n\n"
        f"Missing or blank ({len(missing)} of {len(REQUIRED_ENV_VARS)} required):\n"
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
        "password":  os.environ["SNOWFLAKE_PASSWORD"],
        "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"].strip(),
        "database":  os.environ["SNOWFLAKE_DATABASE"].strip(),
        "schema":    os.environ["SNOWFLAKE_SCHEMA"].strip(),
    }
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
