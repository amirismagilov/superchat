import os
from dataclasses import dataclass


@dataclass
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./omnicrm-demo.db")
    environment: str = os.getenv("APP_ENV", "development")
    demo: bool = os.getenv("DEMO_MODE", "false").lower() == "true"
    local_test: bool = os.getenv("LOCAL_TEST_MODE", "false").lower() == "true"
    encryption_key: str = os.getenv("ENCRYPTION_KEY", "")
    jwt_secret: str = os.getenv("JWT_SECRET", "")
    jwks_url: str = os.getenv("JWT_JWKS_URL", "")
    issuer: str = os.getenv("JWT_ISSUER", "omnicrm-local")
    audience: str = os.getenv("JWT_AUDIENCE", "omnicrm")
    experimental: bool = os.getenv("ENABLE_EXPERIMENTAL", "false").lower() == "true"

    def validate(self):
        if not self.encryption_key:
            raise RuntimeError("ENCRYPTION_KEY must be set; run scripts/init_local.py")
        if not self.jwt_secret and not self.jwks_url:
            raise RuntimeError("Configure JWT_SECRET or JWT_JWKS_URL")
        if self.local_test:
            if self.environment != "development" or self.demo:
                raise RuntimeError("LOCAL_TEST_MODE requires development and DEMO_MODE=false")
            if self.jwks_url or len(self.jwt_secret) < 32:
                raise RuntimeError("LOCAL_TEST_MODE requires a local JWT_SECRET of at least 32 characters and no JWKS")
        if self.environment == "production":
            if self.demo or not self.database_url.startswith("postgresql"):
                raise RuntimeError("Production requires PostgreSQL and DEMO_MODE=false")
            if not self.jwks_url and len(self.jwt_secret) < 32:
                raise RuntimeError("JWT_SECRET must contain at least 32 random characters")
            if self.jwks_url and not self.jwks_url.startswith("https://"):
                raise RuntimeError("Production JWKS requires HTTPS")


settings = Settings()
