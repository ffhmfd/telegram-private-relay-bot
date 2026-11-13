import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import verification


class InstallerPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'etc'
        self.app = self.root / 'app'
        self.state = self.root / 'state'

    def classify(self):
        file = Path(__file__).parent / 'scripts' / 'install_preflight.py'
        self.assertTrue(file.exists(), 'installer cannot distinguish incomplete existing installations')
        spec = importlib.util.spec_from_file_location('install_preflight', file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.first_install(self.config, self.app, self.state)

    def test_genuinely_new_installation_initializes(self):
        self.assertTrue(self.classify())

    def test_complete_config_is_preserved_as_update_even_when_code_missing(self):
        self.config.mkdir()
        (self.config / 'token').write_text('saved-token')
        (self.config / 'config').write_text('VERIFICATION_URL=https://verify.example.com\n')
        self.assertFalse(self.classify())

    def test_lost_token_does_not_overwrite_verification_configuration(self):
        self.config.mkdir()
        path = self.config / 'config'
        original = 'VERIFICATION_URL=https://verify.example.com\nTURNSTILE_SITE_KEY=existing\n'
        path.write_text(original)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.classify()
        self.assertEqual(path.read_text(), original)
        self.assertFalse((self.config / 'token').exists())

    def test_lost_config_or_existing_database_never_reinitializes(self):
        self.state.mkdir()
        (self.state / 'relay.sqlite3').write_bytes(b'existing state')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.classify()
        self.config.mkdir()
        (self.config / 'token').write_text('saved-token')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.classify()
        self.assertEqual((self.state / 'relay.sqlite3').read_bytes(), b'existing state')


class VerificationReadinessTests(unittest.TestCase):
    def test_missing_site_key_is_reported_before_service_start(self):
        self.assertTrue(hasattr(verification, 'check_configuration'), 'installer readiness check is missing')
        with patch.dict('os.environ', {'VERIFICATION_URL': 'https://verify.example.com'}, clear=True):
            with self.assertRaises(SystemExit):
                verification.check_configuration()

    def test_disabled_gate_does_not_require_keys(self):
        self.assertTrue(hasattr(verification, 'check_configuration'), 'installer readiness check is missing')
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(verification.check_configuration())

    def test_readiness_fails_when_http_listener_unavailable(self):
        self.assertTrue(hasattr(verification, 'check_readiness'), 'HTTP readiness check is missing')
        with self.assertRaises(SystemExit):
            verification.check_readiness({'address': ('127.0.0.1', 1), 'site_key': 'test-sitekey'}, attempts=1)

if __name__ == '__main__':
    unittest.main()
