"""Mint an API key for one organization.

Generates a cryptographically secure key, stores only its SHA-256 hash on the
organization row, and prints the raw key exactly once. The raw key is never
persisted or logged -- if it is lost, mint a new one (which rotates the old).

Usage:
    python -m scripts.create_api_key --org nimbus-crm-demo

By default this refuses to overwrite an existing key, so an accidental re-run
cannot silently invalidate a live key. Pass --rotate to replace it on purpose.

Safety: the key is displayed only on an interactive terminal. When stdout or
stdin is not a TTY (e.g. piped, captured, or run from a notebook), the script
exits immediately *before* any database change, so no key is generated or lost.
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from api.auth import generate_api_key, hash_api_key
from api.db import build_engine, build_session_factory
from api.models import Organization

# ANSI: clear scrollback, clear screen, move cursor to top-left.
# Best-effort — legacy Windows consoles may not support it.
CLEAR_SCREEN = "\x1b[2J\x1b[3J\x1b[H"


def _is_tty(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def require_interactive(*, stdin=None, stdout=None) -> None:
    """Raise SystemExit if either stream is not an interactive terminal.

    Must be called BEFORE any database access, so a non-interactive invocation
    never generates or rotates a key.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    if not _is_tty(stdout):
        raise SystemExit(
            "Refusing to print an API key to a non-interactive output "
            "(stdout is not a terminal). Run this command in a real terminal."
        )
    if not _is_tty(stdin):
        raise SystemExit(
            "Refusing to print an API key when stdin is not a terminal "
            "(input may be piped or captured). Run this command interactively."
        )


def show_key_once(raw_key: str, org_slug: str, *, stdout, wait) -> None:
    """Display the key, wait for the operator, then clear the screen."""
    print(
        "API key created. Store it now -- it will not be shown again:\n",
        file=stdout,
    )
    print(f"    {raw_key}\n", file=stdout)
    print(f"Organization: {org_slug}", file=stdout)
    print(
        "Send it as:  Authorization: Bearer <key>   (or  X-API-Key: <key>)",
        file=stdout,
    )

    try:
        wait("\nPress Enter to clear the screen...")
    except EOFError:
        pass  # stdin closed — still clear the screen

    print(CLEAR_SCREEN, end="", file=stdout, flush=True)


async def run(
    org_slug: str,
    rotate: bool,
    *,
    stdin=None,
    stdout=None,
    wait=input,
) -> None:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    # Gate: refuse before touching the database.
    require_interactive(stdin=stdin, stdout=stdout)

    engine = build_engine()
    try:
        async with build_session_factory(engine)() as session:
            org = await session.scalar(
                select(Organization).where(Organization.slug == org_slug)
            )
            if org is None:
                raise SystemExit(f"Organization '{org_slug}' not found")

            if org.api_key_hash is not None and not rotate:
                raise SystemExit(
                    f"Organization '{org_slug}' already has an API key. "
                    "Re-run with --rotate to replace it (invalidates the old key)."
                )

            raw_key = generate_api_key()
            org.api_key_hash = hash_api_key(raw_key)
            await session.commit()
    finally:
        await engine.dispose()

    show_key_once(raw_key, org_slug, stdout=stdout, wait=wait)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint an API key for one organization.")
    parser.add_argument("--org", required=True, help="organization slug")
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="replace an existing key (invalidates the previous one)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.org, args.rotate))


if __name__ == "__main__":
    main()
