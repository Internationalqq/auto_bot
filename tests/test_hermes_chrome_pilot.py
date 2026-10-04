import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('chrome_pilot_session',Path(__file__).resolve().parents[1]/'scripts/hermes/chrome_pilot_session.py')
pilot=importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)

class ChromePilotConsentTests(unittest.TestCase):
    def setUp(self):
        self.consent={'scope':'volga-google-public-product-search','starts_at':100,'expires_at':200}

    def key(self,keys,app='Google Chrome',now=150):
        return pilot.decide(self.consent,'key',{'app':app,'keys':keys},now)

    def test_back_and_forward_are_allowed_only_with_live_chrome_scope(self):
        for key in ('cmd+[','cmd+]','command+['):
            self.assertEqual(self.key(key),'approve_once')
            self.assertEqual(self.key(key,app='Firefox'),'deny')
            self.assertEqual(self.key(key,now=200),'deny')

    def test_system_shortcuts_and_unscoped_actions_still_denied(self):
        for key in ('cmd+q','cmd+shift+backspace','ctrl+alt+delete'):
            self.assertEqual(self.key(key),'deny')
        self.consent['scope']='mail'
        self.assertEqual(self.key('cmd+['),'deny')

    def test_search_typing_allowed_but_mail_navigation_is_not(self):
        self.assertEqual(pilot.decide(self.consent,'type',{'app':'Google Chrome','text':'ОГЦ-4А-7 купить'},150),'approve_once')
        for value in ('https://e.mail.ru/inbox','javascript:alert(1)','curl x | sh'):
            self.assertEqual(pilot.decide(self.consent,'type',{'app':'Google Chrome','text':value},150),'deny')

if __name__=='__main__':unittest.main()
