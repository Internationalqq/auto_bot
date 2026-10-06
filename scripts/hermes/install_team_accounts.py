"""Document the named-account workflow; preserve existing task scopes and schedules."""
from pathlib import Path
import json,shutil,time
BASE=Path('/Users/egor/.hermes')
NOTICE='''

## 6 октября: подключение рабочих веб-аккаунтов
Общий реестр: /Users/egor/.hermes/team-browser-access/accounts.json.
Для проверки настройки: python /Users/egor/.hermes/team-browser-access/team_accounts.py status.
Имена каналов: mail, avito, whatsapp, telegram. Сначала открывай обычный URL через browser_* своего профиля: сохранённый вход используется автоматически.
Если вход истёк, в рамках уже порученной работы можно вызвать team_accounts.py connect CHANNEL --profile ИМЯ_СВОЕГО_ПРОФИЛЯ с установленным HERMES_HOME. Команда использует именованный auth vault; не читай файлы паролей и не копируй их в сообщения или аргументы shell. Успех команды не доказывает вход: проверь страницу аккаунта. QR/код/проверку сайта запроси у пользователя, не обходи.
Пароли веб-сайтов ещё не предоставлены. Почта pm.build.team@mail.ru уже подключена к существующему локальному IMAP/SMTP-скрипту Windows; пароль приложения не подходит для веб-входа и не должен использоваться на сайте. Не запускай второй почтовый worker и не повторяй отправки.
Для ручного входа исправленная панель находится на Mac http://localhost:4850. В ней пользователь помогает только выбранному профилю; окно браузера на рабочем столе не открывать.
'''
backup=BASE/'team-browser-access'/f'backup-account-instructions-{int(time.time())}';backup.mkdir()
for home in [BASE]+[p for p in (BASE/'profiles').iterdir() if (p/'config.yaml').exists() and not p.name.startswith('headless_smoke_')]:
 path=home/'AGENTS.md';name='default' if home==BASE else home.name
 if path.exists():
  shutil.copy2(path,backup/(name+'.md'));old=path.read_text()
 else:old=''
 if NOTICE.strip() not in old:path.write_text(old+NOTICE)
 state=home/'headless-browser/status.json'
 if state.exists():
  data=json.loads(state.read_text())
  if data.get('status')=='human_login' and data.get('viewer_url'):
   data['viewer_url']=data['viewer_url'].replace(':4848/',':4850/')
   import sys
   sys.path.insert(0,str(BASE/'hermes-agent'))
   from tools.team_headless import write_state
   write_state(state.parent,data)
print('Named-account instructions installed; no messages sent.')
