"""Issues the short-lived tokens that the private voice runner requires.

POST /v1/voice/tokens is authenticated with the organization's API key
(get_current_org). Your server calls it and passes the returned token to the
visitor's page. The browser never sees the API key or the signing secret.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status

from api.config import get_settings
from api.deps import get_current_org
from api.models import Organization
from voice.voice_auth import MIN_SECRET_LENGTH, issue_voice_token

router = APIRouter(prefix="/v1/voice", tags=["voice"])


@router.post("/tokens", status_code=status.HTTP_201_CREATED)
async def create_voice_token(
    response: Response,
    org: Organization = Depends(get_current_org),
):
    settings = get_settings()
    secret = settings.voice_token_secret
    if secret is None or len(secret.get_secret_value()) < MIN_SECRET_LENGTH:
        # Fail closed: without a secret no token can be minted or verified.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice tokens are not configured",
        )
    ttl = settings.voice_token_ttl_secs
    token = issue_voice_token(
        secret.get_secret_value().encode("utf-8"), str(org.id), ttl
    )
    response.headers["Cache-Control"] = "no-store"
    return {"token": token, "token_type": "bearer", "expires_in": ttl}
