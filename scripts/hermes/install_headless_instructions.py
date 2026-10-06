"""Apply the user's 6 October browser migration without changing task scopes."""
from pathlib import Path
import json
import os
import shutil
import sys
import time

BASE=Path('/Users/egor/.hermes')
NOTICE='''

## 6 октября 2026: отдельный фоновый браузер

Пользователь поручил перевести веб-работу всех агентов в невидимые независимые браузеры.
Для сайтов, поиска, веб-почты и веб-мессенджеров используй штатные browser_*.
Этот пункт заменяет прежние инструкции использовать только Firefox/Safari/Chrome,
computer_use и общий browser_lock для сайтов. Режим local headless настроен оператором,
твой профиль и сохранённые входы изолированы. Desktop computer_use для сайтов не нужен.
Не переключайся автоматически на общий рабочий стол при ошибке browser_*.
Не открывай чужие каталоги браузера, cookies, CDP, личные профили или вкладки.
Для поиска достаточно публичных страниц; платный search API не обязателен.
browser_snapshot даёт свежие refs, browser_navigate/click/type/scroll работают без курсора.
При agent_browser_busy занята другая задача ЭТОГО агента или пользовательский вход;
сохрани работу, сообщи очередь и повтори позже. Это не отказ пользователя.
После сбоя неизвестного исхода отправки сначала проверь историю, не повторяй сообщение.
CAPTCHA, QR/вход, 403, отказ аккаунта не обходить: укажи требуемый вход или точную ошибку.
Профили новые: при странице входа проси пользователя авторизоваться через подготовленное
окно входа именно этого агента. Не считай канал без входа проверенным. Не запрашивай
пароли/коды в переписке и не копируй сессии других агентов. Telegram также через рабочий
Telegram Web; если не подключён, отметь отдельно. Нативный клиент для веб-задач не занимай.
Никаких новых разрешений на рассылку, покупки, заказы и раскрытие данных этот переход
не даёт: сохраняются прежние пределы поручений. Номер покупателя не передавать.
Состояние браузера хранится в headless-browser/status.json своего HERMES_HOME.
Оператор может получить скрин командой headless_control.py peek <имя профиля>.
'''


if __name__=='__main__':
    backup=BASE/'team-browser-access'/('backup-headless-instructions-'+str(int(time.time())))
    backup.mkdir()
    for home in [BASE]+[p for p in (BASE/'profiles').iterdir() if (p/'config.yaml').exists() and not p.name.startswith('headless_smoke_')]:
        name='default' if home==BASE else home.name
        target=home/'AGENTS.md'
        if target.exists():shutil.copy2(target,backup/(name+'.md'))
        old=target.read_text() if target.exists() else ''
        if '## 6 октября 2026: отдельный фоновый браузер' not in old:
            target.write_text(old+NOTICE)
    os.environ['HERMES_HOME']=str(BASE/'profiles/gulya')
    sys.path.insert(0,str(BASE/'hermes-agent'))
    from cron.jobs import get_job,update_job
    job=get_job('5d2dbdf7247f')
    (backup/'gulya-job.json').write_text(json.dumps(job,ensure_ascii=False))
    prompt=job['prompt'] if NOTICE.strip() in job['prompt'] else job['prompt']+NOTICE
    enabled=[x for x in job.get('enabled_toolsets',[]) if x!='computer_use']
    if 'browser' not in enabled:enabled.append('browser')
    update_job(job['id'],{'prompt':prompt,'enabled_toolsets':enabled})
    print(backup)
