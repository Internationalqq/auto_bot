import unittest
from restore_ivan_chrome import ivan_text,gulya_text,job_prompt
from reconcile_browser_instructions import reconcile
from restore_gulya_firefox import NOTICE


class RoutingTests(unittest.TestCase):
    def test_ivan_conflict_removed_and_scope_kept(self):
        text=ivan_text(reconcile('# Rules\nNo purchases.\n','commercial'))
        self.assertNotIn('свой browser_* headless Chrome',text)
        self.assertIn('No purchases.',text)
        self.assertIn('Google Chrome.app',text)
        self.assertEqual(ivan_text(text),text)

    def test_gulya_uses_same_lock_not_independent(self):
        text=gulya_text(NOTICE)
        self.assertNotIn('не блокирует его поиск',text)
        self.assertIn('под тем же desktop lock',text)
        self.assertIn('browser_turn_queue.py --timeout 900',text)
        self.assertEqual(gulya_text(text),text)

    def test_prompt_preserves_correspondence_instructions(self):
        text='Only current replies.\nДо любых действий в браузере выполни old acquire.\n\nПочта: do not duplicate sends.'
        out=job_prompt(text)
        self.assertNotIn('old acquire',out)
        self.assertIn('Only current replies.',out)
        self.assertIn('do not duplicate sends.',out)


if __name__=='__main__':unittest.main()
