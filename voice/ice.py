"""Read and describe the PIPECAT_ICE_SERVERS environment variable.

The Pipecat runner already parses this variable and applies it to the WebRTC
transport, so nothing here changes how the connection is made. This module only
inspects the value so the bot can log, at startup, whether a remote visitor will
actually be able to connect — otherwise a cloud deployment silently gathers host
candidates only and every call fails with no obvious cause.

Accepted formats (the same ones the runner accepts):
  - a single bare URL:            stun:stun.l.google.com:19302
  - comma-separated bare URLs:    stun:a:3478,turn:b:3478
  - a JSON array of objects:      [{"urls": "turn:b:3478", "username": "u", "credential": "p"}]
"""

import json
from dataclasses import dataclass, field

ICE_ENV_VAR = "PIPECAT_ICE_SERVERS"
ALLOWED_KEYS = {"urls", "username", "credential"}


@dataclass
class IceStatus:
    has_stun: bool = False
    has_turn: bool = False
    urls: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.urls)

    @property
    def ok_for_public_call(self) -> bool:
        """A public call needs at least STUN; TURN is what makes it reliable."""
        return self.has_stun and not self.problems

    def summary(self) -> str:
        if self.problems:
            return "PIPECAT_ICE_SERVERS is invalid: " + "; ".join(self.problems)
        if not self.configured:
            return (
                f"{ICE_ENV_VAR} is not set: the bot will gather host candidates only. "
                "A remote visitor cannot connect. Set STUN, and a TURN relay for "
                "visitors behind strict NAT, before deploying."
            )
        if self.has_turn:
            return (
                f"ICE configured with STUN and TURN ({len(self.urls)} server(s)). "
                "Good for public calls across restrictive NATs."
            )
        return (
            "ICE configured with STUN only. Direct connections will work, but "
            "visitors behind symmetric NAT or mobile CGNAT may fail. Add a TURN "
            "relay for a reliable public demo."
        )


def _classify(url: str, status: IceStatus) -> None:
    scheme = url.split(":", 1)[0].strip().lower()
    status.urls.append(url)
    if scheme == "stun":
        status.has_stun = True
    elif scheme in ("turn", "turns"):
        status.has_turn = True
        # A TURN server also acts as a STUN server for candidate gathering.
        status.has_stun = True
    else:
        status.problems.append(f"unrecognised ICE scheme in {url!r}")


def parse_ice_servers(raw: str | None) -> IceStatus:
    status = IceStatus()
    if raw is None or not raw.strip():
        return status

    text = raw.strip()
    if text.startswith("["):
        try:
            entries = json.loads(text)
        except json.JSONDecodeError as error:
            status.problems.append(f"not valid JSON: {error}")
            return status
        if not isinstance(entries, list):
            status.problems.append("JSON value must be an array of objects")
            return status
        for entry in entries:
            if not isinstance(entry, dict) or "urls" not in entry:
                status.problems.append("each JSON entry needs a 'urls' field")
                continue
            extra = set(entry) - ALLOWED_KEYS
            if extra:
                status.problems.append(
                    f"unexpected key(s) {sorted(extra)}; allowed: {sorted(ALLOWED_KEYS)}"
                )
            urls = entry["urls"]
            for url in [urls] if isinstance(urls, str) else list(urls):
                _classify(str(url), status)
        return status

    for part in text.split(","):
        url = part.strip()
        if url:
            _classify(url, status)
    return status


def describe_ice_env(raw: str | None) -> IceStatus:
    """Parse the raw env value and return its status (never raises)."""
    try:
        return parse_ice_servers(raw)
    except Exception as error:  # defensive: logging must never break startup
        status = IceStatus()
        status.problems.append(f"could not be parsed: {error}")
        return status
