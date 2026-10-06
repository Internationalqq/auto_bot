"""Windows operator console for Mail login in Gulya's isolated Mac browser."""
import getpass,json,subprocess,time
REMOTE='/Users/egor/.hermes/team-browser-access/mail_login_bridge.py'
PYTHON='/Users/egor/.hermes/hermes-agent/venv/bin/python'

def request(payload):
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','mac-mini-hermes',PYTHON,REMOTE],input=json.dumps(payload,ensure_ascii=True)+'\n',capture_output=True,text=True,encoding='utf-8',timeout=40)
    if result.returncode:raise RuntimeError('Нет подключения к Mac. Проверь, что он включён.')
    response=json.loads(result.stdout)
    if not response.get('ok'):raise RuntimeError(response.get('error','Не удалось выполнить шаг'))
    return response['data']

def main():
    print('\nВХОД В РАБОЧУЮ ПОЧТУ — pm.build.team@mail.ru\n')
    print('Это браузер Гули на Mac. Пароли и коды не сохраняются в файлах.\nПароль при вводе не отображается — так и должно быть.\n')
    while True:
        try:
            form=request({'action':'inspect'})
            if form['logged_in']:
                request({'action':'finish','token':form['token']})
                print('\nОткрыты входящие. Вход выполнен; сессия сохранится в профиле Гули.');input('Enter — закрыть окно.');return
            fields=form['fields'];buttons=form['buttons']
            if fields:
                for i,field in enumerate(fields):print(f"Поле {i+1}: {field['label']}")
                choice='1' if len(fields)==1 else input('Номер поля (Enter — обновить): ').strip()
                if not choice:continue
                i=int(choice)-1;label=fields[i]['label']
                if 'ящик' in label.lower() or 'email' in label.lower():
                    value='pm.build.team@mail.ru' if '@' in label or 'email' in label.lower() else 'pm.build.team'
                    print('Подставляю рабочий логин.')
                else:
                    value=getpass.getpass(label+' (пусто — обновить): ')
                    if not value:continue
                request({'action':'fill','token':form['token'],'index':i,'value':value});del value
                form=request({'action':'inspect'});buttons=form['buttons']
            if not buttons:
                print('Нужен другой способ входа или подтверждение на сайте. Не пытаюсь обойти проверку.')
                input('После подтверждения нажми Enter для проверки.');continue
            # Login-only buttons have already been filtered by the Mac bridge.
            # The usual single-field flow requires no numbered-menu interaction.
            if len(buttons)==1:
                index=0
            else:
                for i,button in enumerate(buttons):print(f"{i+1}. {button['label']}")
                choice=input('Номер кнопки (Enter — 1, q — выход): ').strip() or '1'
                if choice.lower()=='q':return
                index=int(choice)-1
            request({'action':'click','token':form['token'],'index':index})
            time.sleep(1)
        except (RuntimeError,ValueError,IndexError,subprocess.TimeoutExpired) as error:
            print('Шаг не выполнен:',error)
            if input('Enter — обновить; q — выход: ').strip().lower()=='q':return

if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:print('\nЗакрыто. Данные не отправлены повторно.')
