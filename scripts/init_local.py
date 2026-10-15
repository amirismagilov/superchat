#!/usr/bin/env python3
"""Create a private local-demo configuration without overwriting existing keys."""

import argparse
import base64
import os
from pathlib import Path
import secrets
import shlex


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / ".env.local")
    args = parser.parse_args()
    target = args.output.absolute()
    values = {
        "APP_ENV": "development",
        "DEMO_MODE": "true",
        "DATABASE_URL": f"sqlite:///{target.parent / 'omnicrm-demo.db'}",
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "JWT_ISSUER": "omnicrm-local",
        "JWT_AUDIENCE": "omnicrm",
        "ENABLE_EXPERIMENTAL": "false",
        "WORKER_SHARD_COUNT": "1",
        "WORKER_SHARDS": "0",
    }
    content = "# Local demo only. Contains secrets: never commit this file.\n"
    content += "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"Existing configuration retained: {target}")
        return
    with os.fdopen(fd, "w") as output:
        output.write(content)
    print(f"Created local demo configuration: {target} (permissions 0600)")
    print("No secrets printed. Run ./scripts/run-local.sh after installing requirements.lock.")


if __name__ == "__main__":
    main()
