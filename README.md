# OmniCRM / Superchat

Self-hosted messaging service and embeddable inbox for corporate CRM systems.
FastAPI, PostgreSQL, durable outbox, provider workers and a framework-independent
web component. This is a technical pilot; operation with 300 real messenger
accounts requires provider acceptance and load testing.

## Channels

| Channel | Account and login | Scope |
| --- | --- | --- |
| Telegram | User account; phone/code/2FA or QR | Text, personal chats, bounded history import; one application registration per company |
| WhatsApp linked device | QR through self-hosted WAHA NOWEB | Experimental adapter; text and available linked-device history |
| WhatsApp Cloud | Business account and Meta credentials | Official API; signed webhooks and approved message templates |
| MAX personal | Existing account; QR or SMS/2FA | Experimental adapter |
| MAX Bot | Bot token | Bot conversations through official API |
| SMS | Self-hosted SMSGate and a working SIM | Android gateway required |

Messenger login and real delivery must be tested with company-owned accounts.
Files, calls, groups, edits, deletes and a guaranteed full archive are outside
the current text messaging implementation.

## Run locally

Docker Engine/Desktop and Compose are required:

```bash
bash scripts/local_start.sh
```

Open the loopback address printed by the launcher, choose the local pilot and
connect accounts. No provider accounts are connected automatically.
`bash scripts/local_stop.sh` stops the pilot while retaining its data.
The local administrative login must remain private.

## Server and automatic updates

Use the [private server profile](docs/server-pilot.md) for an SSH-only pilot.
For an existing installation, [Git automatic updates](docs/git-auto-update.md)
install only the exact `main` commit that passed the repository CI workflow.
The server makes a backup, migrates the database and updates API/worker while
preserving persistent configuration and provider volumes.

PR checks do not deploy. Unknown, pending or failed CI blocks deployment.
Deployment failure preserves its diagnostic state and does not retry forever.
Application rollback does not reverse database migrations.

Credentials, sessions and message databases stay outside Git. Publish only
source files; raw browser HAR files may contain authenticated sessions.

## Development and verification

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python -m pytest -q
python3 tests/update_pilot_smoke.py
node --test tests/frontend_history.mjs
```

The PostgreSQL ownership test uses a separate disposable database supplied by
`FENCING_POSTGRES_URL_FILE`. CI provides one. Browser tests use mocked providers
and Playwright; they do not authenticate or send messages to real accounts.

## Integration and documentation

- [Embed the inbox and issue employee tokens](docs/frontend-integration.md)
- [Telegram account setup and limitations](docs/telegram-adapter.md)
- [WhatsApp and MAX adapters](docs/personal-adapters.md)
- [Official HTTP connectors and SMS](docs/official-connectors.md)
- [Operations, security and persistence](docs/operations.md)
- [Open-source component assessment](docs/opensource-assessment.md)

Project source is MIT-licensed. Dependencies retain their own licenses; see
`LICENSE`, `requirements.lock` and the component assessment.
