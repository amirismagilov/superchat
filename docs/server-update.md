# Update an existing private pilot

[Automatic updates from Git](git-auto-update.md) avoid downloading each release.
For a manual update, create a release:

```bash
python3 scripts/package_release.py --output-dir /tmp/release
```

Place `scripts/Update.command` beside `omnicrm-server-pilot.tar.gz` and its
`.sha256` file. On the administrator's computer:

```bash
OMNICRM_SSH_TARGET=user@your-server bash Update.command
```

If SSH password authentication is intended, also set `OMNICRM_SSH_PASSWORD=1`.
Do not send private keys, server passwords or messenger codes in support chat.

The updater verifies the source manifest and existing source, builds the
candidate before stopping API/worker, creates a private PostgreSQL dump and
applies only a known migration. It preserves `.env.pilot`, the loopback port,
PostgreSQL/WAHA containers, volumes and local files outside the source manifest.
Changed source files block an update to avoid overwriting manual modifications.

`UPDATE_OK` confirms the updater's application health checks. Real messenger
authorization and delivery require separate account tests.

Backups and the application rollback command are printed by the updater.
Rollback restores source and image, retaining additive database changes;
restoring the database dump is a separate operation that can lose newer data.

For read-only WhatsApp diagnosis, put `scripts/Diagnose.command` beside
`scripts/diagnose_whatsapp.py` and run:

```bash
OMNICRM_SSH_TARGET=user@your-server bash Diagnose.command
```

It reports allowlisted states and QR availability, not raw QR data, credentials,
phone numbers or message content. It does not restart or modify accounts.
