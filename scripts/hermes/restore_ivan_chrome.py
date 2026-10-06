"""Apply the user's regular Chrome choice at a stopped Volga boundary."""
from pathlib import Path
import json
import os
import re
import shutil
import sys
import time

MARKER = '# Иван: обычный Google Chrome — 6 октября 2026'
ROUTE = '''
По последнему прямому поручению пользователя Иван выполняет текущий прогон Волги
в существующем обычном Google Chrome (/Applications/Google Chrome.app), через
computer_use app="Google Chrome". Не Google Chrome for Testing и не browser_*.
Не переносить cookies, не создавать профиль, не менять сеть или настройки защиты.
CAPTCHA/QR и явный отказ сайта требуют отчёта, не обхода. Другой браузер не
гарантирует отсутствия CAPTCHA; не повторять запрещённый запрос для обхода.
Гуля использует Firefox. Оба используют один общий desktop browser_lock.
Runner Ивана уже владеет lock на время одной позиции: внутри позиции не получать
его повторно и не освобождать самостоятельно. Между позициями runner освобождает
lock; ожидающей Гуле отдаётся следующая очередь. Пока GUI занят, Иван ждёт без
кликов и переключения окон. Не снимать чужую блокировку, не завершать чужие процессы.
Не использовать Cmd+F: этот шаг нестабилен; читать свежий capture/AX и прокручивать.
Ввод URL — cmd+l, свежий capture, set_value полного адреса, проверка текста, return.
Исходные вкладки, черновики, история результатов и deadline сохраняются.
Пилот ограничен публичным поиском и доказательствами цен: без почты, сообщений,
заказов, оплат, установки ПО и смены системных настроек. Телефон не передавать.
'''
WAIT = '''
Ожидание очереди GUI: до начала обхода запусти
/Users/egor/.hermes/hermes-agent/venv/bin/python -B /Users/egor/.hermes/team-browser-access/browser_turn_queue.py --timeout 900
через terminal с timeout=930. Если инструмент вернул идентификатор фонового процесса,
дождись его результата через штатный process; не запускай второго ожидателя.
Скрипт без GPT ждёт до 15 минут и резервирует следующую очередь после текущей
позиции Ивана. При acquired сохрани ticket и затем release gulya TICKET в finally.
15 минут рабочего обхода отсчитывай ПОСЛЕ получения lock. При timeout/busy сообщи
владельца и причину; ничего не кликай и не снимай чужой lock. Нет разрешения на
бесконечное ожидание или повторный запуск рассылки.
'''


def ivan_text(text):
    for heading in [MARKER, '# Действующий способ веб-доступа — 6 октября 2026',
                    '## 6 октября 2026: отдельный фоновый браузер',
                    '## 6 октября: подключение рабочих веб-аккаунтов']:
        text=re.sub(r'(?ms)^'+re.escape(heading)+r'\n.*?(?=^#{1,2} |\Z)', '', text)
    # Legacy recovery paragraph belongs to the assigned browser, not Gulya's.
    text=text.replace('Вкладки и восстановление Firefox','Вкладки и восстановление Google Chrome')
    text=text.replace('один штатный перезапуск Firefox','один штатный перезапуск Google Chrome')
    return MARKER+'\n'+ROUTE+'\n'+text.strip()+'\n'


def gulya_text(text):
    text=text.replace('Перед GUI получи browser_lock acquire gulya и обязательно освободи свой ticket\nчерез release в finally.',
                      'Перед GUI дождись lock через browser_turn_queue.py по правилам очереди ниже;\nпосле работы обязательно освободи свой ticket через release в finally.')
    text=text.replace('Иван (commercial) продолжает поиск в своём изолированном headless browser_*;\nобщий desktop lock Гули не блокирует его поиск. Не трогай процессы/профиль Ивана.',
                      'Иван (commercial) использует обычный Google Chrome под тем же desktop lock.\nНе трогай его процессы/профиль. Перед началом работы получи очередь по правилам ниже.')
    text=text.replace('Иван использует свой headless без desktop lock;', 'Иван использует обычный Google Chrome под общим lock;')
    if 'browser_turn_queue.py --timeout 900' not in text:
        text+='\n## Очередь Гули и Ивана — 6 октября 2026\n'+WAIT
    return text


def job_prompt(text):
    if 'До любых действий в браузере выполни ' not in text:
        return gulya_text(text)
    start=text.index('До любых действий в браузере выполни ')
    end=text.index('\n\nПочта:',start)
    return gulya_text(text[:start]+WAIT.strip()+text[end:])


def main():
    base=Path('/Users/egor/.hermes');team=base/'team-browser-access'
    root=base/'profiles/commercial/workspace/volga-chrome-pilot-20261004/full-tender-20261004'
    state=json.loads((root/'run-state.json').read_text())
    assert state['status']=='stopped' and (root/'stop-request').exists()
    assert time.time()<state['deadline']
    os.environ['HERMES_HOME']=str(base/'profiles/gulya');sys.path.insert(0,str(base/'hermes-agent'))
    from cron.jobs import get_job,update_job
    job=get_job('5d2dbdf7247f');assert job and not job.get('fire_claim'), 'Wait for the active Gulya job'
    backup=team/('backup-regular-chrome-'+str(int(time.time())));backup.mkdir(mode=0o700)
    for profile,names in [('commercial',['SOUL.md','AGENTS.md']),('gulya',['SOUL.md','AGENTS.md','WORK_CHANNELS.md'])]:
        for name in names:
            p=base/'profiles'/profile/name;shutil.copy2(p,backup/(profile+'-'+name))
            p.write_text(ivan_text(p.read_text()) if profile=='commercial' else gulya_text(p.read_text()))
    p=team/'README.md';shutil.copy2(p,backup/'README.md')
    text=p.read_text();tail=text.split('## История настройки:',1)[1].split('\n',1)[1]
    from restore_gulya_firefox import NOTICE
    from reconcile_browser_instructions import COMMON
    p.write_text('# Текущие браузеры — 6 октября 2026\n'+gulya_text(NOTICE)+'\n'+ROUTE+
                 '\nДля остальных профилей (не gulya/commercial) сохраняется headless:\n'+COMMON+
                 '\n## История настройки: назначения ниже заменены правилами выше\n'+tail)
    (backup/'gulya-job.json').write_text(json.dumps(job,ensure_ascii=False,indent=2))
    updated=update_job(job['id'],{'prompt':job_prompt(job['prompt'])})
    for k in ['schedule','next_run_at','enabled','model','provider','deliver','enabled_toolsets']:
        assert updated.get(k)==job.get(k),k
    print(json.dumps({'backup':str(backup),'gulya_next_run':updated['next_run_at'],'deadline':state['deadline']}))


if __name__=='__main__':main()
