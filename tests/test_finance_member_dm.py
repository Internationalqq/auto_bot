import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('finance_member_dm', Path(__file__).resolve().parents[1] / 'scripts/hermes/finance_member_dm/__init__.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AdmissionTests(unittest.TestCase):
    def event(self, user='42', kind='dm'):
        return NS(source=NS(platform=NS(value='telegram'), chat_type=kind, user_id=user, chat_id=user))

    def member(self, status='member', **extra):
        return {'user': {'id': 42, 'is_bot': False}, 'status': status, **extra}

    def setUp(self):
        self.env = patch.dict(os.environ, HERMES_HOME=str(module.PROFILE), TELEGRAM_ALLOWED_USERS=module.OWNER)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_member_admitted_and_reverified_then_revoked(self):
        with patch.object(module, 'lookup_member', side_effect=[self.member(), self.member('left')]) as lookup, patch.object(module, 'record') as record:
            self.assertIsNone(module.admit_dm(self.event()))
            self.assertIn('42', os.environ['TELEGRAM_ALLOWED_USERS'].split(','))
            self.assertEqual(module.admit_dm(self.event())['action'], 'skip')
            self.assertNotIn('42', os.environ['TELEGRAM_ALLOWED_USERS'].split(','))
            self.assertEqual(lookup.call_count, 2)
            record.assert_called_once()

    def test_api_or_audit_failure_does_not_keep_previous_grant(self):
        for target in ['lookup_member', 'record']:
            with self.subTest(target=target), patch.object(module, 'lookup_member', return_value=self.member()), patch.object(module, 'record'), patch.object(module, target, side_effect=TimeoutError('secret')):
                os.environ['TELEGRAM_ALLOWED_USERS'] = module.OWNER + ',42'
                self.assertEqual(module.admit_dm(self.event())['action'], 'skip')
                self.assertEqual(os.environ['TELEGRAM_ALLOWED_USERS'], module.OWNER)

    def test_no_effect_on_owner_groups_or_other_profiles(self):
        with patch.object(module, 'lookup_member') as lookup:
            self.assertIsNone(module.admit_dm(self.event(module.OWNER)))
            self.assertIsNone(module.admit_dm(self.event(kind='group')))
            os.environ['HERMES_HOME'] = '/other/profile'
            self.assertIsNone(module.admit_dm(self.event()))
            lookup.assert_not_called()

    def test_identity_and_membership_status(self):
        for status in ['creator', 'administrator', 'member']:
            self.assertTrue(module.is_member(self.member(status), '42'))
        self.assertTrue(module.is_member(self.member('restricted', is_member=True), '42'))
        for status in ['restricted', 'left', 'kicked']:
            self.assertFalse(module.is_member(self.member(status), '42'))
        self.assertFalse(module.is_member(self.member(), '99'))
        self.assertFalse(module.is_member({'user': {'id': 42, 'is_bot': True}, 'status': 'member'}, '42'))
        event = self.event(); event.source.chat_id = '99'
        with patch.object(module, 'lookup_member') as lookup:
            self.assertEqual(module.admit_dm(event)['action'], 'skip')
            lookup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
