import json
from dataclasses import dataclass
from functools import lru_cache

import jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings


def seal(value: dict) -> str:
    return Fernet(settings.encryption_key.encode()).encrypt(json.dumps(value).encode()).decode()


def unseal(value: str) -> dict:
    return json.loads(Fernet(settings.encryption_key.encode()).decrypt(value.encode()))


@dataclass
class Identity:
    sub: str
    tenant_id: str
    role: str


@lru_cache
def jwks_client(url):
    return jwt.PyJWKClient(url, cache_keys=False, lifespan=300)


bearer = HTTPBearer(auto_error=False)


def identity(auth: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Identity:
    if auth is None:
        raise HTTPException(401, "authentication_required")
    try:
        if settings.jwks_url:
            key = jwks_client(settings.jwks_url).get_signing_key_from_jwt(auth.credentials).key
            algorithms = ["RS256"]
        else:
            key, algorithms = settings.jwt_secret, ["HS256"]
        claims = jwt.decode(auth.credentials, key, algorithms=algorithms,
                            audience=settings.audience, issuer=settings.issuer,
                            options={"require": ["sub", "tenant_id", "role", "exp", "iat", "iss", "aud"]})
        if not isinstance(claims["role"], str) or claims["role"] not in {"agent", "admin"}:
            raise ValueError("role")
        if any(not isinstance(claims[k], str) or not 1 <= len(claims[k]) <= 128 for k in ("sub", "tenant_id")):
            raise ValueError("identity")
        return Identity(claims["sub"], claims["tenant_id"], claims["role"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "invalid_token") from None
