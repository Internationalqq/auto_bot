"""Apply the user's explicit Avito/Firefox exception to Gulya only."""
from pathlib import Path
import json
import os
import shutil
import sys
import time

MARKER = '## 6 октября: Авито Гули в существующем Firefox'
NOTICE = '''

## 6 октября: Авито Гули в существующем Firefox
Пользователь явно оставил Авито в уже авторизованном Firefox на Mac. Это узкое
исключение из перехода на headless: Авито проверяй штатным computer_use с app=Firefox,
переиспользуй существующую вкладку. 6 октября оператор проверил личный кабинет
«Перспективная Методика» и индикатор сообщений; перед работой заново проверь аккаунт.
Не меняй настройки аккаунта, не экспортируй cookies и не переноси профиль.
Перед Firefox получи общий browser_lock для gulya, освободи в finally. При busy
сохрани причину и продолжи другие доступные каналы. Не снимай чужую блокировку.
Почта pm.build.team@mail.ru и остальные веб-каналы остаются browser_* в своём
изолированном headless-профиле; вход Mail.ru уже проверен после перезапуска.
Ручной вход (human_login) занимает только headless-профиль, не запрещает проверку
Авито в Firefox. Не прерывай QR-вход пользователя, не управляй его панелью.
Иван использует отдельный headless-профиль и не должен получать desktop lock.
Отказ сайта или инструмента, CAPTCHA и повторный вход требуют точного отчёта;
не обходи их. Ограничения отправок, защита от дублей и запрет телефона сохраняются.
'''


def amend(text):
    if MARKER in text:
        return text
    return text + NOTICE


def job_updates(job):
    enabled = list(job.get('enabled_toolsets') or [])
    for name in ['browser', 'computer_use']:
        if name not in enabled:
            enabled.append(name)
    return {'prompt': amend(job['prompt']), 'enabled_toolsets': enabled}


def main():
    base = Path('/Users/egor/.hermes')
    home = base/'profiles/gulya'
    os.environ['HERMES_HOME'] = str(home)
    sys.path.insert(0, str(base/'hermes-agent'))
    from cron.jobs import get_job, update_job
    job = get_job('5d2dbdf7247f')
    assert job and not job.get('fire_claim'), 'Do not modify a claimed job'
    backup = base/'team-browser-access'/('backup-gulya-avito-'+str(int(time.time())))
    backup.mkdir(mode=0o700)
    target = home/'AGENTS.md'
    shutil.copy2(target, backup/'AGENTS.md')
    (backup/'job.json').write_text(json.dumps(job, ensure_ascii=False, indent=2))
    target.write_text(amend(target.read_text()))
    updated = update_job(job['id'], job_updates(job))
    for key in ['schedule', 'next_run_at', 'model', 'provider', 'deliver', 'origin', 'enabled']:
        assert updated.get(key) == job.get(key), key
    print(json.dumps({'backup':str(backup), 'profile':'gulya',
                      'toolsets':updated['enabled_toolsets'], 'schedule_preserved':True}))


if __name__ == '__main__':
    main()
