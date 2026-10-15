# Private server pilot

The pilot lives in `/opt/omnicrm-pilot`, using the `omnicrm-pilot` Compose project.
It binds the API to loopback and is intended for trusted SSH administrators.
It is not the production SSO configuration for a whole sales team.

The server requires Linux, Docker Engine, Compose and sufficient free disk for
images, PostgreSQL and backups. The installer checks for conflicting resources
and chooses a free loopback port in the range 18580–18599.

Create a release with `python3 scripts/package_release.py --output-dir /tmp/release`.
Copy the two release files and `scripts/Deploy.command` to one folder on the
administrator's computer. Run with the actual SSH target:

```bash
OMNICRM_SSH_TARGET=user@your-server bash Deploy.command
```

The installer refuses to overwrite an existing pilot. Use
[automatic Git updates](git-auto-update.md) for an installed pilot, or the
[manual updater](server-update.md).

On the server:

```bash
cd /opt/omnicrm-pilot
bash scripts/server_pilot.sh status
bash scripts/server_pilot.sh stop
bash scripts/server_pilot.sh start
```

Keep `.env.pilot` and all volumes. They contain encryption keys, credentials,
database records and provider sessions. Do not use `down -v` to repair startup.

For an API bound to port 18580, open an authenticated local tunnel:

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 -L 127.0.0.1:18580:127.0.0.1:18580 user@your-server
```

Open `http://127.0.0.1:18580`. Keep the tunnel running. Do not publish this
administrative profile through a public reverse proxy. For multiuser access use
the production profile, HTTPS and employee authentication from the CRM.
