"""API-key-per-organization authentication.

Scope (P0): one API key per organization, used to authenticate the text-chat
HTTP routes and scope them to the caller's organization. This is deliberately
NOT user accounts, roles, sessions or OAuth -- only enough identity to close
the open door the audit found, where a conversation UUID alone authorised
reading and writing a conversation.

Security decisions
------------------
* Keys are generated with `secrets.token_urlsafe(32)` -> 256 bits of entropy.
* Only a SHA-256 hash of the key is stored, never the raw key. For a
  high-entropy random token a fast hash is the correct choice: slow password
  hashes (bcrypt/argon2) exist to defend *low-entropy* human passwords against
  brute force, which does not apply to a 256-bit random value. This matches how
  API-key systems (e.g. GitHub, Stripe) store keys, and keeps per-request
  verification to a single indexed lookup.
* The raw key is shown exactly once, by the minting script, and never logged.
"""

import hashlib
import secrets

# A short, human-recognisable prefix so a leaked key is identifiable and so the
# header value is obviously an API key. It is part of the hashed material.
API_KEY_PREFIX = "vsa_"

# Length of the hex digest stored in the database (SHA-256 -> 64 hex chars).
API_KEY_HASH_LENGTH = 64


def generate_api_key() -> str:
    """Returns a fresh, cryptographically secure raw API key (show once)."""
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(raw_key: str) -> str:
    """Returns the SHA-256 hex digest stored for a raw key.

    The raw key is never persisted or logged; only this digest is.
    """
    return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()
