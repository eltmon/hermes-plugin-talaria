"""CLI: ``hermes t3code login|connect|environments|logout``."""

from __future__ import annotations

import argparse
import sys

from .auth_direct import (
    LoginResult,
    PairingUrlError,
    login,
    logout,
)
from .errors import TalariaError


def _setup_argparse(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes t3code`` argparse tree (google_meet pattern)."""
    subs = subparser.add_subparsers(dest="t3code_command")

    login_p = subs.add_parser(
        "login",
        help="Pair with a T3 Code environment via a t3 pair URL",
    )
    login_p.add_argument(
        "url", nargs="?", default=None, help="Pairing URL from `t3 pair`"
    )
    login_p.add_argument("--name", default=None, help="Environment name to store")

    subs.add_parser(
        "connect",
        help="Sign in to T3 Connect (headless Clerk PKCE)",
    )

    subs.add_parser(
        "environments",
        help="List configured and discovered T3 Code environments",
    )

    logout_p = subs.add_parser(
        "logout",
        help="Remove stored credentials for an environment",
    )
    logout_p.add_argument("env", nargs="?", default=None, help="Environment name")


def t3code_command(args: argparse.Namespace, ctx=None, *, store=None, client=None) -> int:
    sub = getattr(args, "t3code_command", None)
    if not sub:
        print("usage: hermes t3code {login,connect,environments,logout}")
        return 2
    if sub == "login":
        return _cmd_login(args, ctx, store=store, client=client)
    if sub == "logout":
        return _cmd_logout(args, ctx, store=store)
    if sub in ("connect", "environments"):
        print(f"hermes t3code {sub}: not implemented yet")
        return 0
    print(f"unknown subcommand: {sub}")
    return 2


def _cmd_login(args, ctx, *, store=None, client=None) -> int:
    url = getattr(args, "url", None)
    if not url:
        print(
            "usage: hermes t3code login <pairing-url> [--name <env-name>]\n"
            "Paste the pairing URL from `t3 pair`.",
            file=sys.stderr,
        )
        return 0
    if ctx is None:
        print("internal error: plugin context missing", file=sys.stderr)
        return 1
    try:
        result = login(
            url,
            name=getattr(args, "name", None),
            ctx=ctx,
            store=store,
            client=client,
        )
    except PairingUrlError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except TalariaError as exc:
        print(exc.hint or exc.error, file=sys.stderr)
        return 1
    _print_login(result)
    return 0


def _cmd_logout(args, ctx, *, store=None) -> int:
    env = getattr(args, "env", None)
    if not env:
        print("usage: hermes t3code logout <env>", file=sys.stderr)
        return 0
    if ctx is None:
        print("internal error: plugin context missing", file=sys.stderr)
        return 1
    try:
        logout(env, ctx=ctx, store=store)
    except TalariaError as exc:
        print(exc.hint or exc.error, file=sys.stderr)
        return 1
    print(f"Logged out {env!r}")
    return 0


def _print_login(result: LoginResult) -> None:
    print(f"Paired environment {result.name!r}")
    print(f"base_url: {result.base_url}")
    if result.scope:
        print(f"scope: {result.scope}")
    if result.expires_in is not None:
        print(f"expires_in: {result.expires_in}")


def register_cli(ctx) -> None:
    """Register the ``hermes t3code`` command. No network I/O."""

    def handler_fn(args: argparse.Namespace) -> int:
        return t3code_command(args, ctx)

    ctx.register_cli_command(
        name="t3code",
        help="Drive T3 Code environments (login, connect, environments, logout)",
        setup_fn=_setup_argparse,
        handler_fn=handler_fn,
        description=(
            "Pair with a T3 Code environment, sign in to T3 Connect, "
            "list environments, or log out."
        ),
    )
