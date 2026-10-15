#!/usr/bin/env python3
"""Real HTTP + PostgreSQL + demo transport smoke test; no live messenger traffic.

Creates a NEW dedicated database, launches the actual API and worker, then
restarts both and verifies persisted history. Never prints database credentials.
Run with the repository's virtualenv. A PostgreSQL role with CREATEDB is needed.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from cryptography.fernet import Fernet
import httpx
import jwt
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    return round(ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower])
                 * (index - lower), 3)


def distribution(values):
    return {"count": len(values), "p50_ms": percentile(values, .50),
            "p95_ms": percentile(values, .95), "p99_ms": percentile(values, .99),
            "max_ms": round(max(values), 3) if values else None}


def hardware():
    result = {"python": platform.python_version(), "platform": platform.system(),
              "architecture": platform.machine(), "visible_logical_cpus": os.cpu_count()}
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        result["cgroup_cpu_quota_cores"] = None if quota == "max" else int(quota) / int(period)
        memory = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        result["cgroup_memory_limit_mib"] = None if memory == "max" else round(int(memory) / 1048576)
    except (OSError, ValueError):
        pass
    return result


class Metrics:
    def __init__(self):
        self.latencies = defaultdict(list)
        self.statuses = Counter()
        self.errors = Counter()
        self.completed = []

    async def request(self, client, stage, method, path, *, expected=200, **kwargs):
        started = time.perf_counter()
        try:
            response = await client.request(method, path, **kwargs)
            self.statuses[str(response.status_code)] += 1
            if response.status_code != expected:
                self.errors[f"{stage}:http_{response.status_code}"] += 1
                raise RuntimeError(f"{stage}_unexpected_http_status")
            return response.json()
        except httpx.HTTPError as exc:
            self.errors[f"{stage}:{type(exc).__name__}"] += 1
            raise RuntimeError(f"{stage}_transport_error") from None
        finally:
            self.latencies[stage].append((time.perf_counter() - started) * 1000)

    def report(self, seconds):
        all_values = [v for values in self.latencies.values() for v in values]
        return {"elapsed_seconds": round(seconds, 3), "http_requests": len(all_values),
                "http_requests_per_second": round(len(all_values) / seconds, 3),
                "http_statuses": dict(self.statuses), "errors": dict(self.errors),
                "request_latency": distribution(all_values),
                "latency_by_operation": {k: distribution(v) for k, v in self.latencies.items()},
                "operator_workflow": distribution(self.completed)}


class Runtime:
    def __init__(self, environment, port, logs, api_workers):
        self.env, self.port, self.logs = environment, port, logs
        self.api_workers = api_workers
        self.processes = []
        self.handles = []
        self.generation = 0

    def start(self):
        self.generation += 1
        for name, command in [
            ("api", [sys.executable, "-m", "uvicorn", "app.api:app", "--host", "127.0.0.1",
                     "--port", str(self.port), "--workers", str(self.api_workers), "--no-access-log"]),
            ("worker", [sys.executable, "-m", "app.worker"]),
        ]:
            handle = (self.logs / f"{name}-{self.generation}.log").open("w")
            self.handles.append(handle)
            self.processes.append(subprocess.Popen(command, cwd=ROOT, env=self.env,
                                                   stdout=handle, stderr=subprocess.STDOUT,
                                                   start_new_session=True))

    async def ready(self, base_url):
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if any(p.poll() is not None for p in self.processes):
                    raise RuntimeError("child_process_exited_before_ready")
                try:
                    if (await client.get(base_url + "/health/ready")).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.1)
        raise RuntimeError("api_readiness_timeout")

    def stop(self):
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in self.processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        self.processes.clear()
        for handle in self.handles:
            handle.close()
        self.handles.clear()


def database_url(args):
    source = os.getenv("LOAD_TEST_ADMIN_DATABASE_URL")
    if args.database_config:
        if source:
            raise ValueError("choose_database_config_or_environment_url")
        source = json.loads(Path(args.database_config).read_text())["url"]
    if not source:
        raise ValueError("postgres_admin_url_required")
    admin = make_url(source).set(drivername="postgresql", database="postgres")
    if admin.get_backend_name() != "postgresql":
        raise ValueError("postgresql_required")
    if not args.database.startswith("omnicrm_load") or not args.database.replace("_", "").isalnum():
        raise ValueError("database_name_must_start_omnicrm_load")
    with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as connection:
        if connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (args.database,)).fetchone():
            raise ValueError("test_database_already_exists_choose_new_name")
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(args.database)))
    return admin.set(database=args.database, drivername="postgresql+psycopg")


def seed(users):
    from app.db import Account, Base, Conversation, Session, engine
    from app.security import seal
    Base.metadata.create_all(engine)
    actors = []
    with Session.begin() as db:
        for index in range(users):
            sub = f"synthetic-manager-{index:03}"
            account = Account(tenant_id="synthetic-load", owner_id=sub, channel="demo",
                              label=f"Synthetic demo {index}", state="connected", encrypted_config=seal({}))
            db.add(account)
            db.flush()
            conversation = Conversation(account_id=account.id, peer=f"synthetic-customer-{index}",
                                        title=f"Synthetic customer {index}")
            db.add(conversation)
            db.flush()
            claims = {"sub": sub, "tenant_id": "synthetic-load", "role": "agent",
                      "iss": "omnicrm-load", "aud": "omnicrm", "iat": int(time.time()),
                      "exp": int(time.time()) + 3600}
            actors.append({"account_id": account.id, "conversation_id": conversation.id,
                           "token": jwt.encode(claims, os.environ["JWT_SECRET"], algorithm="HS256")})
    engine.dispose()
    return actors


def snapshot(url):
    uri = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(uri) as connection:
        counts = dict(connection.execute("SELECT status,count(*) FROM messages GROUP BY status").fetchall())
        leases = connection.execute("SELECT count(*) FROM shard_leases").fetchone()[0]
        locks = connection.execute("""SELECT count(*) FROM pg_locks WHERE locktype='advisory'
            AND classid=734128 AND granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database())""").fetchone()[0]
        duplicates = connection.execute("""SELECT count(*) FROM (
            SELECT conversation_id,client_id FROM messages WHERE client_id IS NOT NULL
            GROUP BY conversation_id,client_id HAVING count(*)>1) duplicate_groups""").fetchone()[0]
        return {"status_counts": counts, "shard_lease_rows": leases, "advisory_locks": locks,
                "duplicate_client_id_groups": duplicates,
                "postgres_version": connection.execute("SHOW server_version").fetchone()[0]}


async def wait_shards(url, count=16):
    for _ in range(100):
        value = await asyncio.to_thread(snapshot, url)
        if value["advisory_locks"] == count:
            return value
        await asyncio.sleep(.1)
    raise RuntimeError("worker_did_not_acquire_all_shards")


async def workflow(actor, base_url, metrics, gate, timeout):
    async with httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=30,
                                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
                                headers={"Authorization": "Bearer " + actor["token"]}) as client:
        await gate.wait()
        started = time.perf_counter()
        account_id, conversation_id = actor["account_id"], actor["conversation_id"]
        path = f"/api/conversations/{conversation_id}/messages"
        accounts = await metrics.request(client, "accounts", "GET", "/api/accounts")
        if len(accounts) != 1 or accounts[0]["id"] != account_id:
            raise RuntimeError("owner_isolation_failed")
        conversations = await metrics.request(client, "conversations", "GET", "/api/conversations")
        if len(conversations) != 1 or conversations[0]["id"] != conversation_id:
            raise RuntimeError("conversation_isolation_failed")
        if await metrics.request(client, "initial_history", "GET", path):
            raise RuntimeError("new_history_not_empty")
        first = {"text": "Synthetic outgoing one", "client_id": str(uuid4())}
        # Exercise a concurrent race with the same idempotency key, not just replay.
        replies = await asyncio.gather(
            metrics.request(client, "send", "POST", path, expected=202, json=first),
            metrics.request(client, "send_duplicate", "POST", path, expected=202, json=first),
        )
        if replies[0]["id"] != replies[1]["id"]:
            raise RuntimeError("idempotency_failed")
        second = await metrics.request(client, "send", "POST", path, expected=202,
                                       json={"text": "Synthetic outgoing two", "client_id": str(uuid4())})
        actor["message_ids"] = [replies[0]["id"], second["id"]]
        await metrics.request(client, "inbound", "POST", "/api/demo/inbound",
                              json={"conversation_id": conversation_id, "text": "Synthetic incoming"})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            history = await metrics.request(client, "history_poll", "GET", path)
            if len(history) != 3:
                raise RuntimeError("unexpected_history_count")
            outgoing = [m for m in history if m["direction"] == "outbound"]
            inbound = [m for m in history if m["direction"] == "inbound"]
            if len(outgoing) != 2 or len(inbound) != 1:
                raise RuntimeError("incorrect_message_direction")
            if any(m["status"] in {"failed", "uncertain"} for m in outgoing):
                raise RuntimeError("outbox_not_successful")
            if all(m["status"] == "sent" for m in outgoing):
                if {m["id"] for m in outgoing} != set(actor["message_ids"]):
                    raise RuntimeError("message_identity_changed")
                actor["history_ids"] = sorted(m["id"] for m in history)
                metrics.completed.append((time.perf_counter() - started) * 1000)
                return
            await asyncio.sleep(.5)
        raise RuntimeError("outbox_completion_timeout")


async def verify_restart(actors, base_url, timeout):
    metrics = Metrics()
    started = time.perf_counter()

    async def verify(actor):
        async with httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=30,
                                    headers={"Authorization": "Bearer " + actor["token"]}) as client:
            history = await metrics.request(client, "restart_history", "GET",
                                             f'/api/conversations/{actor["conversation_id"]}/messages')
            if sorted(m["id"] for m in history) != actor["history_ids"]:
                raise RuntimeError("history_not_persisted")
            if sum(m["status"] == "sent" for m in history) != 2:
                raise RuntimeError("status_not_persisted")

    results = await asyncio.gather(*(verify(a) for a in actors), return_exceptions=True)
    if any(isinstance(r, BaseException) for r in results):
        raise RuntimeError("restart_history_verification_failed")
    actor = actors[0]
    async with httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=30,
                                headers={"Authorization": "Bearer " + actor["token"]}) as client:
        created = await metrics.request(client, "restart_send", "POST",
                                        f'/api/conversations/{actor["conversation_id"]}/messages', expected=202,
                                        json={"text": "Synthetic after restart", "client_id": str(uuid4())})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = await metrics.request(client, "restart_delivery", "GET", f'/api/messages/{created["id"]}')
            if msg["status"] == "sent":
                return metrics.report(time.perf_counter() - started)
            await asyncio.sleep(.5)
    raise RuntimeError("restarted_worker_did_not_send")


async def run(args, report):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))
    url = database_url(args)
    report["database"] = args.database  # No URL, username, password, token, or key.
    environment = os.environ.copy()
    environment.update({"APP_ENV": "development", "DEMO_MODE": "true", "ENABLE_EXPERIMENTAL": "false",
                        "DATABASE_URL": url.render_as_string(hide_password=False),
                        "ENCRYPTION_KEY": Fernet.generate_key().decode(), "JWT_SECRET": secrets.token_urlsafe(48),
                        "JWT_JWKS_URL": "", "JWT_ISSUER": "omnicrm-load", "JWT_AUDIENCE": "omnicrm",
                        "WORKER_SHARD_COUNT": "16", "WORKER_SHARDS": ",".join(str(i) for i in range(16)),
                        "WORKER_CONCURRENCY": "32", "ACCOUNT_POLL_SECONDS": str(args.poll_seconds)})
    # app settings are imported only after installing this isolated test config.
    os.environ.update(environment)
    actors = seed(args.users)
    logs = Path(tempfile.mkdtemp(prefix="omnicrm-load-logs-"))
    report["process_log_directory"] = str(logs)
    environment["WORKER_HEARTBEAT_PATH"] = str(logs / "heartbeat")
    runtime = Runtime(environment, args.port, logs, args.api_workers)
    base_url = f"http://127.0.0.1:{args.port}"
    try:
        runtime.start()
        await runtime.ready(base_url)
        report["postgres_before"] = await wait_shards(url)
        metrics, gate = Metrics(), asyncio.Event()
        tasks = [asyncio.create_task(workflow(actor, base_url, metrics, gate, args.timeout)) for actor in actors]
        await asyncio.sleep(0)  # All operators reach the start barrier.
        started = time.perf_counter()
        gate.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.perf_counter() - started
        report["load"] = metrics.report(elapsed)
        failures = Counter(str(result) for result in outcomes if isinstance(result, BaseException))
        report["workflow_errors"] = dict(failures)
        report["successful_operators"] = len(metrics.completed)
        report["outbound_messages_per_second"] = round(args.users * 2 / elapsed, 3)
        report["postgres_after_load"] = await asyncio.to_thread(snapshot, url)
        if failures:
            raise RuntimeError("operator_workflows_failed")
        counts = report["postgres_after_load"]["status_counts"]
        if counts != {"sent": args.users * 2, "received": args.users}:
            raise RuntimeError("database_message_counts_incorrect")
        if report["postgres_after_load"]["duplicate_client_id_groups"] != 0:
            raise RuntimeError("database_contains_duplicates")
        runtime.stop()
        runtime.start()
        await runtime.ready(base_url)
        await wait_shards(url)
        report["restart"] = await verify_restart(actors, base_url, args.timeout)
        report["postgres_after_restart"] = await asyncio.to_thread(snapshot, url)
        report["passed"] = True
    finally:
        runtime.stop()
        # The dedicated database is deliberately retained, never auto-dropped.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-config", help="Private JSON file with an admin PostgreSQL URL in key 'url'")
    parser.add_argument("--database", default="omnicrm_load_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--users", type=int, default=300)
    parser.add_argument("--api-workers", type=int, default=2)
    parser.add_argument("--port", type=int, default=8878)
    parser.add_argument("--poll-seconds", type=float, default=3)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--report", default=str(ROOT / "docs" / "load-test-results.json"))
    args = parser.parse_args()
    if not 1 <= args.users <= 1000 or args.poll_seconds <= 0 or not 1 <= args.api_workers <= 4:
        parser.error("users must be 1–1000; polling interval must be positive; API workers must be 1–4")
    report = {"test": "synthetic_demo_transport_real_http_postgres_worker",
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "passed": False,
              "hardware": hardware(), "concurrent_operators": args.users,
              "configuration": {"api_processes": args.api_workers, "transport_workers": 1, "worker_concurrency": 32,
                                "worker_shards": 16, "poll_seconds": args.poll_seconds,
                                "demo_mode": True, "tls": False, "network": "loopback"},
              "scope_limit": "No live messenger sessions, provider rate limits, WAN, attachments or sustained soak workload."}
    try:
        asyncio.run(run(args, report))
    except Exception as exc:
        # Exception text from database libraries can contain credentials.
        report["fatal_error_type"] = type(exc).__name__
    finally:
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "operators": report.get("successful_operators", 0),
                      "report": str(output), "fatal_error_type": report.get("fatal_error_type")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
