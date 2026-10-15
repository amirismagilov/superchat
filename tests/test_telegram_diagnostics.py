"""Read-only diagnostic regressions: no credentials, identifiers, or network calls."""
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('telegram_diagnostic', ROOT / 'scripts/diagnose_telegram.py')
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class TelegramDiagnosticTests(unittest.TestCase):
    def test_account_job_and_environment_output_are_strictly_redacted(self):
        current = datetime.now(timezone.utc)
        account = SimpleNamespace(id='PRIVATE_ACCOUNT_ID', tenant_id='PRIVATE_TENANT', state='SECRET_STATE', label='SECRET_LABEL', encrypted_config='SECRET_ENCRYPTED_CONFIG', updated_at=current - timedelta(seconds=25), public={'error': 'SECRET_ERROR_WITH_PHONE', 'phone': 'SECRET_PHONE', 'qr_text': 'SECRET_QR', 'identity': {'id': 'SECRET_TELEGRAM_ID'}, 'retrying': 'SECRET_UNEXPECTED_VALUE'})
        job = SimpleNamespace(action='SECRET_ACTION', status='SECRET_STATUS', error='SECRET_JOB_ERROR', created_at=current, encrypted_payload='SECRET_CODE_PASSWORD')
        result = diag.account_summary(account, [job] * 5, 1, True, current)
        result['environment'] = diag.environment_summary({'DATABASE_URL': 'SECRET_CONNECTION_URL', 'ENCRYPTION_KEY': 'SECRET_ENCRYPTION_KEY', 'WORKER_HEARTBEAT_PATH': '/SECRET_PATH', 'TELEGRAM_API_HASH': 'SECRET_API_HASH', 'TELEGRAM_PROXY_URL': 'socks5://SECRET_CREDENTIALS@proxy.invalid:1080'})
        output = json.dumps(result)
        for sensitive in ('SECRET', account.id, account.tenant_id):
            self.assertNotIn(sensitive, output)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['public_error'], 'unknown_error')
        self.assertEqual(result['updated_age_seconds'], 25)
        self.assertEqual(len(result['jobs']), 3)
        self.assertTrue(all(type(value) is bool for value in result['environment'].values()))
        self.assertTrue(result['environment']['telegram_proxy_configured'])

    def test_known_errors_actions_and_states_are_preserved(self):
        current = datetime.now(timezone.utc)
        account = SimpleNamespace(id='account', state='awaiting_code', public={'error': 'telegram_transport_error'}, updated_at=current.replace(tzinfo=None))
        job = SimpleNamespace(action='login', status='queued', error='worker_interrupted_retry_login', created_at=current - timedelta(seconds=200))
        result = diag.account_summary(account, [job], 1, False, current)
        self.assertEqual(result['state'], 'awaiting_code')
        self.assertEqual(result['public_error'], 'telegram_transport_error')
        self.assertEqual(result['jobs'][0], {'action': 'login', 'status': 'queued', 'error': 'worker_interrupted_retry_login', 'age_seconds': 200})
        self.assertFalse(result['shared_configuration_exists'])
        self.assertEqual(diag.safe_error('provider_auth_timeout'), 'provider_auth_timeout')
        self.assertEqual(diag.safe_error('telegram_proxy_configuration_invalid'), 'telegram_proxy_configuration_invalid')

    def test_heartbeat_is_metadata_only_and_absence_does_not_mean_worker_down(self):
        current = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'SECRET_HEARTBEAT_PATH'
            path.write_text('SECRET_CONTENT_NEVER_READ')
            os.utime(path, (current.timestamp() - 15, current.timestamp() - 15))
            with patch.object(Path, 'read_text', side_effect=AssertionError('Heartbeat contents must never be read')):
                result = diag.heartbeat_summary(current, {'WORKER_HEARTBEAT_PATH': str(path)})
            self.assertEqual(result, {'status': 'visible', 'age_seconds': 15, 'fresh': True})
            self.assertNotIn('SECRET', json.dumps(result))
            path.unlink()
            self.assertEqual(diag.heartbeat_summary(current, {'WORKER_HEARTBEAT_PATH': str(path)})['status'], 'not_visible_in_this_process')
            secret = Path(folder) / 'private-file'; secret.write_text('DO_NOT_READ')
            path.symlink_to(secret)
            self.assertEqual(diag.heartbeat_summary(current, {'WORKER_HEARTBEAT_PATH': str(path)})['status'], 'not_regular_file')

    def test_exception_text_never_reaches_output(self):
        output = io.StringIO()
        with patch.object(diag, 'diagnose', side_effect=RuntimeError('SECRET_CONNECTION_CREDENTIALS')), patch.object(diag.logging, 'disable'), contextlib.redirect_stdout(output):
            status = diag.main()
        self.assertEqual(status, 1)
        self.assertNotIn('SECRET', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['result'], 'diagnostic_unavailable')

    def test_missing_or_unsupported_database_does_not_create_default_sqlite_file(self):
        for environment, status in [({}, 'database_not_configured'), ({'DATABASE_URL': 'sqlite:///SECRET_NEW_FILE.db'}, 'database_backend_not_supported')]:
            with self.subTest(status=status), patch.dict(os.environ, environment, clear=True):
                report = diag.diagnose()
            self.assertEqual(report['result'], status)
            self.assertNotIn('SECRET', json.dumps(report))

    def test_database_projection_limits_tenant_flags_read_only_and_no_provider_network(self):
        from sqlalchemy import Column, DateTime, Integer, JSON, String, create_engine
        from sqlalchemy.orm import declarative_base, Session
        Base = declarative_base()
        class Account(Base):
            __tablename__ = 'accounts'
            id = Column(String, primary_key=True)
            tenant_id = Column(String)
            channel = Column(String)
            state = Column(String)
            public = Column(JSON)
            updated_at = Column(DateTime(timezone=True))
            encrypted_config = Column(String)
            label = Column(String)
        class Job(Base):
            __tablename__ = 'jobs'
            id = Column(Integer, primary_key=True)
            account_id = Column(String)
            action = Column(String)
            status = Column(String)
            error = Column(String)
            created_at = Column(DateTime(timezone=True))
            encrypted_payload = Column(String)
        class IntegrationSetting(Base):
            __tablename__ = 'integration_settings'
            tenant_id = Column(String, primary_key=True)
            channel = Column(String)
            encrypted_config = Column(String)
        engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(engine)
        current = datetime.now(timezone.utc)
        with Session(engine) as db:
            db.add(IntegrationSetting(tenant_id='PRIVATE_TENANT_CONFIGURED', channel='telegram', encrypted_config='SECRET_SHARED_CREDENTIALS'))
            for index in range(13):
                db.add(Account(id='PRIVATE_ACCOUNT_' + str(index), tenant_id='PRIVATE_TENANT_CONFIGURED' if index % 2 == 0 else 'PRIVATE_TENANT_OTHER', channel='telegram' if index < 12 else 'whatsapp_web', state='connecting', public={'phone': 'SECRET_PHONE', 'error': 'telegram_transport_error'}, updated_at=current - timedelta(seconds=index), encrypted_config='SECRET_ACCOUNT_SESSION', label='SECRET_LABEL'))
                for number in range(5):
                    db.add(Job(account_id='PRIVATE_ACCOUNT_' + str(index), action='login', status='queued', created_at=current - timedelta(seconds=number), encrypted_payload='SECRET_OTP'))
            db.commit()
        module = ModuleType('app.db')
        module.Account, module.Job, module.IntegrationSetting = Account, Job, IntegrationSetting
        statements = []
        class ReadOnlyProbe:
            rolled_back = False
            def __init__(self, session):
                self.session = session
            def get_bind(self):
                return SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
            def execute(self, query):
                statement = str(query)
                statements.append(statement)
                if statement.startswith('SET '):
                    return None
                return self.session.execute(query)
            def scalar(self, query):
                statements.append(str(query))
                return self.session.scalar(query)
            def rollback(self):
                self.rolled_back = True
                self.session.rollback()
        with Session(engine) as db, patch.dict(sys.modules, {'app.db': module}), patch('socket.create_connection', side_effect=AssertionError('No provider connection allowed')):
            probe = ReadOnlyProbe(db)
            result = diag.read_accounts(probe, current)
        engine.dispose()
        self.assertTrue(probe.rolled_back)
        self.assertEqual(statements[0], 'SET TRANSACTION READ ONLY')
        self.assertEqual(statements[1], "SET LOCAL statement_timeout = '5000ms'")
        self.assertTrue(all(statement.startswith(('SELECT ', 'SET ')) for statement in statements))
        for secret_column in ('encrypted_config', 'encrypted_payload', 'label'):
            self.assertNotIn(secret_column, '\n'.join(statements))
        self.assertEqual(result['accounts_total'], 12)
        self.assertTrue(result['accounts_truncated'])
        self.assertEqual(len(result['accounts']), 10)
        self.assertTrue(all(len(account['jobs']) == 3 for account in result['accounts']))
        self.assertEqual([account['shared_configuration_exists'] for account in result['accounts']], [True, False] * 5)
        self.assertTrue(result['shared_configuration_exists'])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('PRIVATE_', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
