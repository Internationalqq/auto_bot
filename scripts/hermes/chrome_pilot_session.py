"""Supply the user's renewed, time-limited Chrome consent to one Hermes session.

Uses the CLI's existing approval callback. Does not edit Hermes or grant shell,
mail, other-app, permanent, or session-wide approval.
"""
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit


GOOGLE_AI_DISCOVERY_POLICY = '''
## Сначала сайты через режим ИИ Google — поручение пользователя 7 октября 2026
Для очередной позиции сначала используй доступный в текущем Google «Режим ИИ»
(AI Mode) как поиск сайтов поставщиков, затем проверяй сами сайты. Это относится
только к поиску цен Ивана в Google Chrome под своим browser_lock, не к Гуле.
В повторном проходе учти previous-evidence и ищи именно недостающие сведения,
не начинай весь тендер заново и не открывай уже отклонённые недоступные сайты.
Открой режим только через реально наблюдаемую кнопку/ссылку свежего capture.
Запрос: точное название и артикул, важные характеристики, единица и объём,
город поставки из задания. Попроси дать прямые ссылки на карточки товара или
прайсы поставщиков, по возможности с опубликованной ценой и нужным регионом.
Например: «Найди сайты поставщиков [товар, артикул, характеристики] для поставки
в [город]. Дай прямые ссылки на карточки или прайсы, где можно проверить цену
за [единицу], наличие и НДС. Отдельно пометь аналоги и отличия».
Передавай только сведения о товаре и регионе из рабочей спецификации: не весь
внутренний файл, переписку, персональные данные, учётные данные или закрытые КП.
Пиши компактно, одной строкой до 1000 символов, через свежий элемент поля ввода.
Если список неточный, можно задать до двух коротких уточнений: точная модель,
другая характеристика или регион, «дай другие сайты, кроме [проверенные]».
Как только есть подходящие ссылки, переходи к ним; не трать сессию на разговор
с ИИ. Если после уточнений новых подходящих ссылок нет — обычная выдача Google.
Если режима ИИ просто нет в интерфейсе либо он недоступен без отдельного входа,
не включай его сменой профиля, аккаунта, сети или браузера: используй обычную
выдачу, только если она доступна и нет явного отказа/проверки доступа. CAPTCHA,
401/403, квота или явный отказ не обходятся переходом в другой режим.
Переходи по ссылке из свежего capture или её наблюдаемому URL. Упомянутый ИИ
выдуманный адрес не восстанавливай догадкой. Каждый источник отдельно открой,
сверь точную модель/размер/комплектность, цену и единицу, НДС, наличие, регион
и доставку. Цифры, утверждения и ссылки в ответе ИИ — подсказки, не доказательство
цены или соответствия. Если первичная страница не подтверждает цену — цена
не проверена. Публичная карточка не становится подтверждением поставщика.
В result.json в attempts запиши использование google_ai_discovery, запросы,
наблюдаемые ссылки и причину возврата к обычной выдаче, если он потребовался.
В offers сохраняй доказательства только прочитанных первичных карточек/прайсов;
не переноси цену из ИИ, обзора или сниппета. Сохрани результаты текущей позиции
до перехода к следующей. Лимиты сессии, deadline, быстрый пропуск недоступных
сайтов и уборка завершённых вкладок остаются в силе. Сообщения поставщикам
в мониторинге/публичной проверке не отправлять.
'''


COMPLETED_TABS_POLICY = '''
## Уборка завершённых вкладок — поручение пользователя 7 октября 2026
Пользователь поручил убрать накопившиеся просмотренные вкладки Ивана и Гули.
Это заменяет прежний запрет закрывать исходные вкладки ТОЛЬКО для завершённой
рабочей страницы: вкладку можно закрыть и из прошлой сессии после проверки.
Работай только под своим общим browser_lock в назначенном браузере:
Иван — Google Chrome, Гуля — Firefox. Не переключайся на браузер другого агента.
В начале следующей работы сначала разбери накопившиеся вкладки; затем убирай
завершённые страницы перед освобождением lock. Используй свежие capture и
наблюдаемые элементы списка вкладок; проверяй активную страницу перед cmd+w.
Не закрывай вкладки пачкой по координатам или одному обрезанному заголовку.
Закрывай просмотренные публичные карточки/прайсы/выдачи, если их URL и результат
уже сохранены в рабочей истории и они не нужны для текущего незавершённого шага.
Иван: история — run-state.json рядом с каталогом позиции и result.json завершённых
batch-*; проверяй относящиеся к вкладке записи, не перечитывай всю историю в ответ.
Недоступную публичную страницу также закрывай после сохранения URL и ошибки.
Дубликаты закрывай только после проверки одинакового URL и сохранения одной
нужной рабочей вкладки. Не создавай новые вкладки для уборки.
Сохраняй закреплённые и личные/чужие вкладки, рабочие чаты и почту, формы,
черновики, незавершённые отправки/загрузки, страницы входа и CAPTCHA.
Само наличие пустой формы поиска на публичной странице не является черновиком;
но заполненная форма заказа/обращения — причина сохранить вкладку.
Если назначение или завершённость не доказаны, оставь вкладку с конкретной
причиной. «Исходная вкладка» сама по себе больше НЕ причина сохранять завершённую
рабочую страницу. Не закрывай последнее окно/вкладку, не используй cmd+q,
cmd+shift+w, массовое закрытие или перезапуск браузера ради уборки.
Перед закрытием запиши URL, название и основание в tab-cleanup.json; после
каждого cmd+w новым capture проверь исчезновение вкладки и оставшиеся страницы.
При запросе подтверждения потери данных отменяй закрытие, сохраняй вкладку.
В отчёте уборки укажи число вкладок до/после, закрытые URL и причины сохранения.
Не утверждай, что вкладки закрыты, пока это не подтверждено свежим capture.
'''


# These prefixes are browser search syntax, not navigation URI schemes.
SEARCH_OPERATORS = {'site', 'filetype', 'ext', 'intitle', 'allintitle', 'inurl',
                    'allinurl', 'intext', 'allintext', 'before', 'after'}
READING_KEYS = {
    'cmd+l', 'cmd+a', 'cmd+t', 'cmd+r', 'cmd+f', 'cmd+g', 'cmd+shift+g',
    'cmd+[', 'cmd+]', 'ctrl+tab', 'ctrl+shift+tab', 'cmd+shift+[', 'cmd+shift+]',
    'cmd+1', 'cmd+2', 'cmd+3', 'cmd+4', 'cmd+5', 'cmd+6', 'cmd+7', 'cmd+8', 'cmd+9',
    'enter', 'return', 'escape', 'tab', 'shift+tab', 'backspace', 'delete',
    'left', 'right', 'down', 'up', 'home', 'end', 'pageup', 'pagedown',
    'space', 'shift+space', 'shift+left', 'shift+right', 'cmd+left', 'cmd+right',
    'cmd+up', 'cmd+down', 'cmd+shift+left', 'cmd+shift+right',
    'cmd+-', 'cmd+=', 'cmd++', 'cmd+0',
}


def reading_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        return False
    if any(ord(c) < 32 for c in value):
        return False
    value = value.strip()
    prefix = re.match(r'^([a-z][a-z0-9+.-]*):', value, re.I)
    if prefix and prefix.group(1).lower() not in SEARCH_OPERATORS:
        try:
            u = urlsplit(value)
            return bool(u.scheme == 'https' and u.hostname and not u.username and not u.password
                        and not any(s in u.hostname for s in
                                    ('mail.', 'accounts.', 'account.', 'web.telegram.', 'web.whatsapp.')))
        except ValueError:
            return False
    return not any(c in value for c in (';', '|', '`', '\\'))


def chrome_help_strip(window, state):
    sc = state.get('structuredContent') or {}
    bounds = window.get('bounds') or sc.get('window_bounds') or {}
    return (window.get('app_name') == 'Google Chrome' and not window.get('title')
            and sc.get('elements') == []
            and 0 < bounds.get('height', 0) <= 24
            and bounds.get('width', 0) > 0)


def chrome_omnibox_popup(window, state):
    elements = (state.get('structuredContent') or {}).get('elements') or []
    return (window.get('app_name') == 'Google Chrome' and not window.get('title')
            and any(e.get('role') == 'AXWebArea' and e.get('label') == 'Omnibox Popup' for e in elements)
            and not any(e.get('role') in ('AXDialog', 'AXSheet') for e in elements))


def chrome_find_popup(window, state):
    sc = state.get('structuredContent') or {}
    elements = sc.get('elements') or []
    bounds = window.get('bounds') or sc.get('window_bounds') or {}
    return (window.get('app_name') == 'Google Chrome' and not window.get('title')
            and 24 < bounds.get('height', 0) <= 140 and 0 < bounds.get('width', 0) <= 800
            and any(e.get('role') == 'AXWindow' and e.get('label', '').split('\n')[0].strip() in
                    ('Найти на странице', 'Find in page') for e in elements)
            and any(e.get('role') == 'AXTextField' and e.get('label') in ('Найти', 'Find') for e in elements)
            and any(e.get('role') == 'AXButton' and e.get('label') in
                    ('Закрыть панель поиска', 'Close find bar') for e in elements)
            and not any(e.get('role') in ('AXDialog', 'AXSheet') for e in elements))


def chrome_pointer_args(action, args):
    # AXPress (even PX hit-test -> AX) does not establish renderer focus.
    # Use the driver's documented foreground pixel rung for an explicitly
    # requested pixel click, never synthesize clicks or drop the exact window.
    result = dict(args)
    if (action == 'click' and args.get('pid') is not None and args.get('window_id') is not None
            and args.get('x') is not None and args.get('y') is not None
            and args.get('element_index') is None and args.get('element_token') is None):
        result['delivery_mode'] = 'foreground'
    return result


def select_chrome_content(windows, select):
    """Skip at most two observed Chrome overlays, proving tooltip identity.

    Screenshot pixel sizes change with display scaling. The empty hover
    strip must instead match an AXHelpTag's logical frame in the main tree.
    Never skip an unknown overlay or select another process's window.
    """
    selected, state = select(windows)
    pid = selected.get('pid')
    skipped = []
    strips = []
    for _ in range(3):
        strip = chrome_help_strip(selected, state)
        omnibox = chrome_omnibox_popup(selected, state)
        find_bar = chrome_find_popup(selected, state)
        if not strip and not omnibox and not find_bar:
            if not skipped:
                return selected, state
            elements = (state.get('structuredContent') or {}).get('elements') or []
            main = any(e.get('role') == 'AXWindow' and 'Google Chrome' in e.get('label', '') for e in elements)
            help_frames = [e.get('frame') or {} for e in elements if e.get('role') == 'AXHelpTag']
            def matches(bounds, frame):
                return all(abs(bounds.get(a, -9999) - frame.get(b, 9999)) <= 1
                           for a,b in [('x','x'),('y','y'),('width','w'),('height','h')])
            if not main or any(not any(matches(bounds,frame) for frame in help_frames) for bounds in strips):
                raise RuntimeError('Unrecognized Chrome overlay; content selection stopped')
            return selected, state
        skipped.append(selected['window_id'])
        if strip:
            strips.append(selected.get('bounds') or (state.get('structuredContent') or {}).get('window_bounds') or {})
        remaining = [w for w in windows if w['window_id'] not in skipped and w['pid'] == pid]
        if not remaining:
            break
        selected, state = select(remaining)
    raise RuntimeError('Chrome overlays have no verified content window')


def decide(consent, action, args, now):
    if consent.get('scope') != 'volga-google-public-product-search':
        return 'deny'
    if not consent.get('starts_at', 0) <= now < consent.get('expires_at', 0):
        return 'deny'
    if args.get('app') != 'Google Chrome':
        return 'deny'
    if action in ('type', 'set_value'):
        value = args.get('text', args.get('value', ''))
        if not reading_text(value):
            return 'deny'
    elif action == 'key':
        key = args.get('keys', '').lower().replace('command', 'cmd').replace('control', 'ctrl').replace(' ', '')
        if key == 'cmd+w':
            return 'approve_once' if consent.get('mode') == 'full_tender' and consent.get('allow_own_tab_cleanup') is True else 'deny'
        if key not in READING_KEYS:
            return 'deny'
    elif action not in {'click', 'scroll', 'focus_app'}:
        return 'deny'
    return 'approve_once'


def main():
    batch = Path(sys.argv[1]).resolve()
    root = batch.parent
    consent_path = root / 'chrome-consent.json'
    consent = json.loads(consent_path.read_text())
    if not consent['starts_at'] <= time.time() < consent['expires_at']:
        raise SystemExit('Chrome consent expired')
    if consent.get('mode') == 'full_tender':
        manifest = json.loads((root / 'input.json').read_text())['source']
        rows = manifest['positions']
        assert manifest['tender_id'] == '0171200001926000664'
        assert 1 <= len(rows) <= 2000
        match = re.fullmatch(r'batch-([1-9][0-9]*)(?:-attempt-(2))?', batch.name)
        assert match
        index = int(match.group(1)) - 1
        assert 0 <= index < len(rows)
        assert json.loads((batch / 'positions.json').read_text()) == [rows[index]]
    else:
        assert batch.name in {f'batch-{i}' for i in range(1, 11)}
    assert os.environ['HERMES_HOME'] == '/Users/egor/.hermes/profiles/commercial'
    import cli
    expected_path = root / 'expected-model.json'
    if expected_path.exists():
        expected = json.loads(expected_path.read_text())
        from hermes_cli.config import load_config
        configured = load_config()
        assert configured['model']['default'] == expected['model']
        assert cli.CLI_CONFIG['agent']['reasoning_effort'] == expected['reasoning_effort']
        original_cli_init = cli.HermesCLI.__init__
        def checked_cli_init(self, *args, **kwargs):
            original_cli_init(self, *args, **kwargs)
            assert self.reasoning_config.get('effort') == expected['reasoning_effort']
            (batch/'runtime-model.json').write_text(json.dumps({
                'model':configured['model']['default'], 'reasoning':self.reasoning_config}))
        cli.HermesCLI.__init__ = checked_cli_init
    from tools.computer_use.cua_backend import CuaDriverBackend
    original_select = CuaDriverBackend._select_content_window
    original_init = CuaDriverBackend.__init__
    original_action = CuaDriverBackend._action

    def init_chrome(self, *args, **kwargs):
        if 'Google Chrome' in (kwargs.get('allowed_apps') or []):
            # Keep this price-search pilot narrower than the profile's general
            # permissions (which may also permit Telegram for other tasks).
            kwargs['allowed_apps'] = ['Google Chrome']
            # Supported exact-window foreground delivery, process-local only.
            kwargs['keyboard_delivery_mode'] = 'foreground'
        original_init(self, *args, **kwargs)

    CuaDriverBackend.__init__ = init_chrome

    def select_content(self, windows):
        return select_chrome_content(windows, lambda candidates: original_select(self, candidates))

    CuaDriverBackend._select_content_window = select_content

    def focused_pointer(self, action, args):
        return original_action(self, action, chrome_pointer_args(action, args))

    CuaDriverBackend._action = focused_pointer

    def approved(self, action, args, summary):
        verdict = decide(consent, action, args, time.time())
        audit = {'at': time.time(), 'action': action, 'app': args.get('app'), 'verdict': verdict}
        if action == 'key':
            audit['keys'] = args.get('keys')
        with (batch / 'approval-audit.jsonl').open('a') as log:
            log.write(json.dumps(audit) + '\n')
        return verdict

    cli.HermesCLI._computer_use_approval_callback = approved
    prompt = (batch / 'prompt.txt').read_text() + '''
Уточнение по проверенному поведению Chrome: переходы по результатам Google
делай click по element индексу ссылки из свежего capture, не по координатам
уменьшенной картинки (они могут попасть мимо). Если клик не перешёл, бери
наблюдаемый URL ссылки из дерева и открывай через cmd+l, свежий capture,
set_value адресной строки полным URL, capture с точным совпадением значения,
return и проверку перехода. Посимвольный type для адресной строки не используй. Не
придумывай URL. После одной карточки сразу сохрани результат; неподходящий
товар или сайт с ошибкой не должны съесть всё время проверки позиции.
'''
    if consent.get('mode') == 'full_tender':
        prompt += '''
Уточнение пользователя 7 октября 2026: быстрый пропуск недоступного сайта.
Это заменяет общий запрет повторного открытия явно недоступных страниц
ТОЛЬКО для обычного сбоя загрузки: ERR_CONNECTION_CLOSED/RESET/TIMED_OUT,
пустая страница или зависшая загрузка. После первого такого сбоя запомни
время и URL. Сделай максимум ДВА обычных обновления cmd+r в общем окне
10 секунд (примерно сразу и через 5 секунд), после каждого свежий capture.
Если страница загрузилась, сразу читай её; второе обновление уже не нужно.
Если 10 секунд прошли, не начинай новый повтор, даже если успел только один.
Вызовы инструментов могут занять дольше: не добавляй ожидание сверх этого окна.
Если загрузки нет, сразу cmd+[ к выдаче/списку и свежий capture, затем
следующая подходящая ссылка на ДРУГОЙ сайт из реально видимого списка.
Запиши ошибку, URL, число обновлений и время в result.json и пометь весь сайт
недоступным до конца ЭТОЙ позиции. Не открывай его главную, каталог, другие
карточки, зеркала и поддомены; не ищи обходной URL того же поставщика.
Если назад не вернуло к списку, открой ранее наблюдаемый URL выдачи обычным
проверенным вводом; не выдумывай URL. Предыдущие источники дорабатывай только
пока сайт доступен. Если подходящие другие сайты закончились, сохрани честный
итог без цены и закончи позицию, не расходуй остаток сессии на этот сайт.
CAPTCHA, DDoS-проверка, HTTP401/403, TLS/сертификат, требование входа,
квоты/лимиты или явный отказ инструмента НЕ являются обычным сбоем загрузки:
для них эти обновления не разрешены; сохрани причину и следуй правилам остановки.
Не меняй VPN, сеть, браузер, профиль или разрешения. Сбой одного сайта сам по
себе не означает browser_error: Chrome доступен, можно читать другой сайт.
'''
        prompt += COMPLETED_TABS_POLICY
        prompt += GOOGLE_AI_DISCOVERY_POLICY
    cli.main(query=prompt, quiet=True,
             toolsets='computer_use,file', max_turns=60 if consent.get('mode') == 'full_tender' else 24)


if __name__ == '__main__':
    main()
