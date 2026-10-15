"""Safety regressions for the copyable read-only diagnostic, without provider access."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("whatsapp_diagnostic", ROOT / "scripts/diagnose_whatsapp.py")
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class Response:
    def __init__(self, body, status=200):
        self.status_code = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_bytes(self):
        yield self.body


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def stream(self, method, path, params=None):
        self.calls.append((method, path, params))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class DiagnosticTests(unittest.TestCase):
    def account(self, **kwargs):
        return SimpleNamespace(id="private-account-uuid", public={}, state="connecting", updated_at=datetime.now(timezone.utc), encrypted_config="private-encrypted-blob", **kwargs)

    def test_no_secret_output_and_get_only(self):
        account = self.account()
        account.public = {"error": "SECRET_EXCEPTION_WITH_PHONE", "provider_state": "SECRET_PROVIDER_STATE", "qr_text": "SECRET_QR_FROM_DATABASE", "qr_data_url": "SECRET_QR_IMAGE", "identity": {"phone": "SECRET_PHONE", "display_name": "SECRET_NAME"}, "unexpected": "SECRET_ARBITRARY_FIELD"}
        account.label = "SECRET_LABEL"
        job = SimpleNamespace(action="SECRET_ACTION", status="SECRET_STATUS", error="SECRET_JOB_ERROR", created_at=account.updated_at, encrypted_payload="SECRET_OTP")
        session_id = "omni-012345678901234567890123456789ab"
        summary, internal_id = diag.account_summary(account, [job], 1, lambda _: {"_waha_session": session_id, "api_key": "SECRET_API_KEY", "wa_self": {"phone": "SECRET_PHONE"}}, account.updated_at)
        client = Client([
            Response([{"name": session_id, "status": "SCAN_QR_CODE", "me": {"phone": "SECRET_GATEWAY_PHONE"}, "config": {"key": "SECRET_SESSION_KEY"}}]),
            Response({"value": "SECRET_QR_VALUE", "extra": "SECRET_QR_EXTRA"}),
        ])
        gateway = diag.gateway_summary(client, [(summary, internal_id)])
        output = json.dumps({"accounts": [summary], "gateway": gateway})
        self.assertNotIn("SECRET", output)
        self.assertNotIn(account.id, output)
        self.assertNotIn(account.encrypted_config, output)
        self.assertNotIn(session_id, output)
        self.assertEqual(summary["public_error"], "unknown_error")
        self.assertTrue(summary["public_qr_present"])
        self.assertTrue(summary["gateway"]["qr_present"])
        self.assertEqual([call[0] for call in client.calls], ["GET", "GET"])
        self.assertEqual(client.calls[0], ("GET", "/api/sessions", {"all": "true"}))

    def test_known_errors_retained_unknown_values_redacted(self):
        account = self.account()
        account.public = {"error": "waha_connect_timeout", "provider_state": "STARTING"}
        jobs = [SimpleNamespace(action="login", status="failed", error="worker_interrupted_retry_login", created_at=account.updated_at)]
        summary, _ = diag.account_summary(account, jobs, 1, lambda _: {}, account.updated_at)
        self.assertEqual(summary["public_error"], "waha_connect_timeout")
        self.assertEqual(summary["jobs"][0]["error"], "worker_interrupted_retry_login")
        self.assertEqual(summary["session_reference"], "missing_or_invalid")
        self.assertEqual(summary["updated_age_seconds"], 0)

    def test_qr_checks_bounded_and_unrelated_sessions_not_queried(self):
        summaries = [({}, "session-" + str(i)) for i in range(6)]
        records = [{"name": name, "status": "SCAN_QR_CODE"} for _, name in summaries]
        records.append({"name": "other-private-account", "status": "SCAN_QR_CODE"})
        client = Client([Response(records), *[Response({"value": "SECRET_QR"}) for _ in range(diag.MAX_QR_CHECKS)]])
        result = diag.gateway_summary(client, summaries)
        self.assertEqual(result["qr_checks"], diag.MAX_QR_CHECKS)
        self.assertEqual(len(client.calls), 1 + diag.MAX_QR_CHECKS)
        self.assertEqual(summaries[-1][0]["gateway"]["qr_check"], "limit_reached")
        self.assertFalse(any("other-private-account" in call[1] for call in client.calls))

    def test_gateway_errors_never_print_raw_response_or_exception(self):
        for response, status in [(Response({"secret": "SECRET_KEY"}, 401), "http_error"), (Response(b"SECRET_NOT_JSON"), "invalid_json"), (RuntimeError("SECRET_ENDPOINT_WITH_CREDENTIALS"), "request_failed"), (Response(b"x" * (diag.MAX_RESPONSE_BYTES + 1)), "response_too_large")]:
            with self.subTest(status=status):
                summary = {}
                result = diag.gateway_summary(Client([response]), [(summary, "private-session")])
                output = json.dumps({"result": result, "summary": summary})
                self.assertNotIn("SECRET", output)
                self.assertNotIn("private-session", output)
                self.assertEqual(result["status"], status)

    def test_absent_or_ambiguous_sessions_do_not_start_sessions(self):
        summary = {}
        client = Client([Response([])])
        diag.gateway_summary(client, [(summary, "private-session")])
        self.assertEqual(summary["gateway"]["status"], "session_not_found")
        self.assertEqual(len(client.calls), 1)
        duplicate = {"name": "private-session", "status": "SCAN_QR_CODE"}
        client = Client([Response([duplicate, duplicate])])
        diag.gateway_summary(client, [(summary, "private-session")])
        self.assertEqual(summary["gateway"]["status"], "ambiguous_session")
        self.assertEqual(len(client.calls), 1)

    def test_decrypt_exception_and_top_level_exception_redacted(self):
        def bad_unseal(_):
            raise ValueError("SECRET_FERNET_KEY")
        summary, session = diag.account_summary(self.account(), [], 1, bad_unseal, datetime.now(timezone.utc))
        self.assertIsNone(session)
        self.assertEqual(summary["session_reference"], "decrypt_failed")
        stdout = io.StringIO()
        with patch.object(diag, "diagnose", side_effect=RuntimeError("SECRET_CONNECTION_STRING")), patch.object(diag.logging, "disable"), contextlib.redirect_stdout(stdout):
            result = diag.main()
        self.assertEqual(result, 1)
        self.assertNotIn("SECRET", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["result"], "diagnostic_unavailable")

    def test_mac_wrapper_streams_source_once_without_remote_install(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("Diagnose.command", "diagnose_whatsapp.py"):
                (folder / name).write_bytes((ROOT / "scripts" / name).read_bytes())
            bin_dir = folder / "bin"
            bin_dir.mkdir()
            ssh = bin_dir / "ssh"
            ssh.write_text('#!/usr/bin/env python3\nimport json,os,pathlib,sys\npathlib.Path(os.environ["TEST_RECORD"]).write_text(json.dumps({"args":sys.argv[1:],"stdin":sys.stdin.read()}))\n')
            ssh.chmod(0o755)
            env = dict(os.environ, PATH=str(bin_dir) + ":" + os.environ["PATH"], TEST_RECORD=str(folder / "record.json"), OMNICRM_SSH_PASSWORD="1", OMNICRM_SSH_TARGET="root@example.test")
            process = subprocess.run(["bash", str(folder / "Diagnose.command")], env=env, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            record = json.loads((folder / "record.json").read_text())
            self.assertIn("PubkeyAuthentication=no", record["args"])
            self.assertEqual(record["args"][-2], "root@example.test")
            remote = record["args"][-1]
            self.assertIn("exec -T -e PYTHONDONTWRITEBYTECODE=1 api python -", remote)
            self.assertNotIn(" up ", remote)
            self.assertNotIn("restart", remote)
            self.assertNotIn("tee ", remote)
            self.assertEqual(record["stdin"], (folder / "diagnose_whatsapp.py").read_text())


if __name__ == "__main__":
    unittest.main()
