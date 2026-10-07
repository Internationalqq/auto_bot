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

    def test_search_operators_are_queries_not_uri_schemes(self):
        for value in ('site:orion76.ru укладка геотекстиля цены',
                      '  SITE:orion76.ru прайс  ', 'filetype:pdf прайс труба',
                      'intitle:прайс inurl:price щебень', '-site:example.com цена',
                      'after:2026-01-01 "кабель 4х150"'):
            for action in ('type', 'set_value'):
                field = 'text' if action == 'type' else 'value'
                self.assertEqual(pilot.decide(self.consent,action,{'app':'Google Chrome',field:value},150),'approve_once')
                self.assertEqual(pilot.decide(self.consent,action,{'app':'Safari',field:value},150),'deny')
                self.assertEqual(pilot.decide(self.consent,action,{'app':'Google Chrome',field:value},200),'deny')

    def test_unsafe_navigation_stays_denied_including_leading_spaces(self):
        for value in (' javascript:alert(1)', 'data:text/html,hello', 'file:///etc/passwd',
                      'chrome://settings', 'https://user:pass@example.com', 'https://[bad',
                      'site:example.com\nhello', 'site:example.com; command', 'https://e.mail.ru/inbox'):
            self.assertFalse(pilot.reading_text(value), value)
        self.assertTrue(pilot.reading_text('https://shop.example/product?q=4%20x%20150'))

    def test_routine_reading_keys_keep_browser_scope(self):
        for key in ('cmd+g','cmd+shift+g','ctrl+tab','cmd+2','pageup','home',
                    'cmd+0','cmd+-','cmd+=','cmd+shift+left'):
            self.assertEqual(self.key(key),'approve_once')
            self.assertEqual(self.key(key,app='Firefox'),'deny')
            self.assertEqual(self.key(key,now=200),'deny')
        for key in ('cmd+s','cmd+p','cmd+q','cmd+alt+i','cmd+shift+delete'):
            self.assertEqual(self.key(key),'deny')

class ChromeOverlayTests(unittest.TestCase):
    def test_ai_omnibox_editor_keeps_own_window_and_snapshot(self):
        self.states[1]['structuredContent']['elements'].append(
            {'role':'AXTextArea','label':'Задайте вопрос','token':'popup-question'})
        selected, state = pilot.select_chrome_content(self.windows, self.select)
        self.assertEqual(selected['window_id'], 1)
        self.assertIs(state, self.states[1])
        self.assertEqual(state['structuredContent']['elements'][-1]['token'], 'popup-question')

    def test_pixel_focus_uses_exact_window_and_never_rewrites_ax_clicks(self):
        args={'pid':10,'window_id':3,'x':500,'y':400,'button':'left'}
        routed=pilot.chrome_pointer_args('click',args)
        self.assertEqual(routed,dict(args,delivery_mode='foreground'))
        self.assertNotIn('delivery_mode',args)
        for action,other in [('click',{'pid':10,'x':500,'y':400}),
                             ('click',dict(args,element_index=2)),
                             ('click',{'pid':10,'window_id':3,'element_index':2}),
                             ('scroll',args)]:
            self.assertEqual(pilot.chrome_pointer_args(action,other),other)

    def test_find_bar_selects_same_process_main_window_without_dismissing_it(self):
        find=dict(app_name='Google Chrome',pid=10,window_id=4,title='')
        self.states[4]={'structuredContent':{'window_bounds':{'height':84,'width':403},'elements':[
            {'role':'AXWindow','label':'Найти на странице\n    "Без имени"'},
            {'role':'AXTextField','label':'Найти'},
            {'role':'AXButton','label':'Закрыть панель поиска'}]}}
        selected,_=pilot.select_chrome_content([find,self.windows[2]],self.select)
        self.assertEqual(selected['window_id'],3)
        self.states[4]['structuredContent']['elements'].append({'role':'AXDialog'})
        selected,_=pilot.select_chrome_content([find,self.windows[2]],self.select)
        self.assertEqual(selected['window_id'],4)

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
