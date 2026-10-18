from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import urlsplit


class AuthConfigurationError(RuntimeError):
    pass


class InvalidAuthToken(ValueError):
    pass


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=4)
def _jwks_client(url: str):
    try:
        import jwt
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise AuthConfigurationError("Install PyJWT[crypto] to verify Neon Auth tokens") from exc
    return jwt.PyJWKClient(url)


def authenticated_user_id(authorization: str | None) -> str | None:
    """Verify a Neon Auth bearer token and return its immutable user subject.

    Auth remains optional for local development until NEON_AUTH_JWKS_URL is set.
    Deployments should set MATCHING_REQUIRE_AUTH=true so a missing configuration
    fails closed rather than accepting the development user id.
    """
    jwks_url = (os.getenv("NEON_AUTH_JWKS_URL") or "").strip()
    base_url = (os.getenv("NEON_AUTH_BASE_URL") or "").strip().rstrip("/")
    parsed_base_url = urlsplit(base_url)
    issuer = f"{parsed_base_url.scheme}://{parsed_base_url.netloc}" if parsed_base_url.netloc else ""
    require_auth = _enabled(os.getenv("MATCHING_REQUIRE_AUTH"))

    if not jwks_url:
        if require_auth:
            raise AuthConfigurationError("NEON_AUTH_JWKS_URL is required when MATCHING_REQUIRE_AUTH=true")
        return None

    if not authorization or not authorization.lower().startswith("bearer "):
        raise InvalidAuthToken("Sign in with Google to continue")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise InvalidAuthToken("The authorization token is empty")

    try:
        import jwt

        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token).key
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm not in {"EdDSA", "ES256", "RS256"}:
            raise InvalidAuthToken("The authorization token uses an unsupported algorithm")
        options = {"verify_aud": False}
        decode_kwargs = {"issuer": issuer} if issuer else {}
        claims = jwt.decode(token, signing_key, algorithms=[algorithm], options=options, **decode_kwargs)
    except InvalidAuthToken:
        raise
    except Exception as exc:
        raise InvalidAuthToken("Your session is invalid or expired") from exc

    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise InvalidAuthToken("The authorization token has no user id")
    return subject
