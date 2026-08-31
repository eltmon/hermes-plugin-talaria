"""CLI: ``hermes t3code login|connect|environments|logout``.

WI-4 owns the real login; this module only registers the command tree.
"""

from __future__ import annotations

import argparse


def _setup_argparse(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes t3code`` argparse tree (google_meet pattern)."""
    subs = subparser.add_subparsers(dest="t3code_command")

    login = subs.add_parser(
        "login",
        help="Pair with a T3 Code environment via a t3 pair URL",
    )
    login.add_argument("url", nargs="?", default=None, help="Pairing URL from `t3 pair`")
    login.add_argument("--name", default=None, help="Environment name to store")

    subs.add_parser(
        "connect",
        help="Sign in to T3 Connect (headless Clerk PKCE)",
    )

    subs.add_parser(
        "environments",
        help="List configured and discovered T3 Code environments",
    )

    logout = subs.add_parser(
        "logout",
        help="Remove stored credentials for an environment",
    )
    logout.add_argument("env", nargs="?", default=None, help="Environment name")

    subparser.set_defaults(func=t3code_command)


def t3code_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "t3code_command", None)
    if not sub:
        print("usage: hermes t3code {login,connect,environments,logout}")
        return 2
    print(f"hermes t3code {sub}: not implemented yet")
    return 0


def register_cli(ctx) -> None:
    """Register the ``hermes t3code`` command. No network I/O."""
    ctx.register_cli_command(
        name="t3code",
        help="Drive T3 Code environments (login, connect, environments, logout)",
        setup_fn=_setup_argparse,
        handler_fn=t3code_command,
        description=(
            "Pair with a T3 Code environment, sign in to T3 Connect, "
            "list environments, or log out."
        ),
    )
