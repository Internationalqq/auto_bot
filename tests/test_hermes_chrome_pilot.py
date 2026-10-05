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

    def test_reload_requires_current_chrome_search_consent(self):
        for key in ('cmd+r', 'command+r'):
            self.assertEqual(self.key(key), 'approve_once')
            self.assertEqual(self.key(key, app='Firefox'), 'deny')
            self.assertEqual(self.key(key, app='Safari'), 'deny')
            self.assertEqual(self.key(key, now=200), 'deny')
        self.consent['scope']='mail'
        self.assertEqual(self.key('cmd+r'), 'deny')

    def test_find_in_page_requires_current_chrome_search_consent(self):
        for key in ('cmd+f', 'command+f'):
            self.assertEqual(self.key(key), 'approve_once')
            self.assertEqual(self.key(key, app='Firefox'), 'deny')
            self.assertEqual(self.key(key, app='Safari'), 'deny')
            self.assertEqual(self.key(key, now=200), 'deny')
        self.consent['scope']='mail'
        self.assertEqual(self.key('cmd+f'), 'deny')

    def test_search_keeps_tab_and_window_close_shortcuts_blocked(self):
        for key in ('cmd+w','command+w','cmd+shift+w','cmd+q'):
            self.assertEqual(self.key(key), 'deny')
        self.assertEqual(self.key('cmd+l'), 'approve_once')
        self.assertEqual(self.key('cmd+['), 'approve_once')

    def test_tab_cleanup_requires_explicit_full_run_consent(self):
        self.consent.update(mode='full_tender',allow_own_tab_cleanup=True)
        for key in ('cmd+w','command+w'):
            self.assertEqual(self.key(key), 'approve_once')
            self.assertEqual(self.key(key,app='Firefox'), 'deny')
            self.assertEqual(self.key(key,app='Safari'), 'deny')
            self.assertEqual(self.key(key,now=200), 'deny')
        for key in ('cmd+shift+w','cmd+q'):
            self.assertEqual(self.key(key), 'deny')
        self.consent.pop('mode')
        self.assertEqual(self.key('cmd+w'), 'deny')
        self.consent.update(mode='full_tender',scope='mail')
        self.assertEqual(self.key('cmd+w'), 'deny')

    def test_search_typing_allowed_but_mail_navigation_is_not(self):
        self.assertEqual(pilot.decide(self.consent,'type',{'app':'Google Chrome','text':'ОГЦ-4А-7 купить'},150),'approve_once')
        for value in ('https://e.mail.ru/inbox','javascript:alert(1)','curl x | sh'):
            self.assertEqual(pilot.decide(self.consent,'type',{'app':'Google Chrome','text':value},150),'deny')

class ChromeOverlayTests(unittest.TestCase):
    def setUp(self):
        self.windows=[dict(app_name='Google Chrome',pid=10,window_id=1,title='',bounds={'height':178,'width':1574}),
                      dict(app_name='Google Chrome',pid=10,window_id=2,title='',bounds={'x':-1,'y':979,'height':22,'width':194}),
                      dict(app_name='Google Chrome',pid=10,window_id=3,title='Product - Google Chrome')]
        self.states={1:{'structuredContent':{'elements':[{'role':'AXWebArea','label':'Omnibox Popup'}]}},
                     2:{'structuredContent':{'elements':[],'screenshot_height':44,'screenshot_width':388}},
                     3:{'structuredContent':{'elements':[{'role':'AXWindow','label':'Product - Google Chrome'},
                                                       {'role':'AXHelpTag','frame':{'x':-1,'y':979,'h':22,'w':194}}]}}}

    def select(self,windows):
        return windows[0],self.states[windows[0]['window_id']]

    def test_two_simultaneous_overlays_with_scaled_screenshot(self):
        selected,_=pilot.select_chrome_content(self.windows,self.select)
        self.assertEqual(selected['window_id'],3)

    def test_normalized_window_geometry_comes_from_capture(self):
        for window in self.windows:
            if 'bounds' in window:
                self.states[window['window_id']]['structuredContent']['window_bounds']=window.pop('bounds')
        selected,_=pilot.select_chrome_content(self.windows,self.select)
        self.assertEqual(selected['window_id'],3)

    def test_empty_strip_requires_matching_tooltip_in_main_tree(self):
        self.states[3]['structuredContent']['elements'].pop()
        with self.assertRaisesRegex(RuntimeError,'Unrecognized'):
            pilot.select_chrome_content(self.windows,self.select)

    def test_never_switches_to_different_process(self):
        self.windows[2]['pid']=99
        with self.assertRaises(RuntimeError):
            pilot.select_chrome_content(self.windows,self.select)

    def test_real_dialog_is_not_skipped(self):
        self.states[1]['structuredContent']['elements'].append({'role':'AXDialog'})
        selected,_=pilot.select_chrome_content(self.windows,self.select)
        self.assertEqual(selected['window_id'],1)

if __name__=='__main__':unittest.main()
