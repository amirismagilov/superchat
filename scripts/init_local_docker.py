#!/usr/bin/env python3
"""Create private, persistent credentials for the isolated local Docker stack."""

import argparse
import base64
import os
from pathlib import Path
import platform
import secrets
import stat


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / ".env.local-docker")
    parser.add_argument("--port", default=os.getenv("LOCAL_PORT", "8080"))
    parser.add_argument("--architecture", default=platform.machine())
    args = parser.parse_args()
    if not args.port.isdigit() or not 1024 <= int(args.port) <= 65535:
        parser.error("LOCAL_PORT must be an integer between 1024 and 65535")
    target = args.output.absolute()
    if target.is_symlink():
        parser.error("Refusing a symlink configuration file")
    if target.exists():
        if not target.is_file():
            parser.error("Configuration path is not a regular file")
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        print(f"Existing {target.name} retained; keys and database password unchanged.")
        return
    # Docker Registry manifests checked 2026-10-14. These are separate native
    # images, not a multi-platform tag; select once and retain the immutable pin.
    waha_images = {
        "amd64": "devlikeapro/waha:noweb-2026.9.2@sha256:be61f8cd7774ab22bd1b755cbc539f98d71d81d5ca83555fdddfa4fddd7924e1",
        "arm64": "devlikeapro/waha:noweb-arm-2026.9.2@sha256:c7fba52db50312c8d0d05536f11ef136ac851c6b2baa11a883ea2d3d6d877f82",
    }
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(args.architecture, args.architecture)
    if architecture not in waha_images:
        parser.error("Local WAHA bundle supports amd64/x86_64 or arm64/aarch64 Docker hosts")
    password = secrets.token_urlsafe(36)
    values = {
        "LOCAL_PORT": str(int(args.port)),
        "POSTGRES_PASSWORD": password,
        "DATABASE_URL": "postgresql+psycopg://omnicrm_local:" + password
                        + "@postgres:5432/omnicrm_local",
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "WAHA_API_KEY": secrets.token_urlsafe(36),
        "WAHA_IMAGE": waha_images[architecture],
        "SMS_GATEWAY_URL": "",
    }
    content = "# Private local account test credentials. Never publish or regenerate for an existing DB.\n"
    content += "\n".join(key + "=" + value for key, value in values.items()) + "\n"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Another launcher already created the configuration; existing keys retained.")
        return
    with os.fdopen(fd, "w") as output:
        output.write(content)
    print(f"Created {target.name} with permissions 0600; no secrets printed.")


if __name__ == "__main__":
    main()
