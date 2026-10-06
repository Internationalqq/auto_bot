import unittest
from restore_gulya_firefox import restore, job_updates, MARKER
from reconcile_browser_instructions import reconcile


class RestoreTests(unittest.TestCase):
    def test_replaces_conflicting_soul_preserves_business_rules(self):
        source = reconcile('# Деловые правила\nНикаких заказов. Не повторять отправку.\n', 'gulya')
        result = restore(source)
        self.assertNotIn('Не переключайся на Firefox для почты', result)
        self.assertNotIn('Используй штатные browser_*', result)
        self.assertIn('Никаких заказов. Не повторять отправку.', result)
        self.assertEqual(restore(result), result)

    def test_job_preserves_schedule_and_sending_scope(self):
        job = {'prompt':'Отвечай только по текущим перепискам.\n\n## 6 октября 2026: отдельный фоновый браузер\nИспользуй browser_*.\n',
               'enabled_toolsets':['terminal','file','browser','computer_use'], 'schedule':{'expr':'old'}}
        change = job_updates(job)
        self.assertEqual(set(change), {'prompt','enabled_toolsets'})
        self.assertEqual(change['enabled_toolsets'], ['terminal','file','computer_use'])
        self.assertIn('Отвечай только по текущим перепискам.', change['prompt'])
        self.assertNotIn('Используй browser_*.', change['prompt'])
        self.assertEqual(job['schedule'], {'expr':'old'})

    def test_replaces_account_helper_and_avito_exception(self):
        source = '## Recovery\nKeep recovery.\n\n## 6 октября: подключение рабочих веб-аккаунтов\nUse vault.\n\n## 6 октября: Авито Гули в существующем Firefox\nMail stays headless.\n'
        result = restore(source)
        self.assertIn('Keep recovery.', result)
        self.assertNotIn('Use vault.', result)
        self.assertNotIn('Mail stays headless.', result)
        self.assertEqual(result.count(MARKER),1)


if __name__ == '__main__':
    unittest.main()
