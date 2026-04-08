from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.config import settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(key: str | None = Security(api_key_header)) -> None:
    """
    Require a valid X-API-Key header.

    If settings.api_key is empty (local dev), auth is bypassed so you don't
    lock yourself out before the env var is configured.
    """
    if not settings.api_key:
        return  # dev bypass
    if key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
