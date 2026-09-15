"""Resolve the existing caller's CRM session; never use a service-account login."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from autobot.uploaded_corrections import CorrectionError, actor_from_user


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve(headers):
    base = str(os.environ.get('PMBI_CRM_URL') or '').strip().rstrip('/')
    address = urllib.parse.urlsplit(base)
    if address.scheme not in {'http', 'https'} or not address.hostname or address.username or address.password or address.query or address.fragment:
        raise CorrectionError('Не настроена проверка пользователя PM.bi. Сохранение недоступно.', 503)
    forwarded = {name: headers.get(name) for name in ('Cookie', 'Authorization') if headers.get(name)}
    if not forwarded:
        raise CorrectionError('Войдите в PM.bi, чтобы проверить смету.', 401)
    if any(len(value) > 16384 or '\r' in value or '\n' in value for value in forwarded.values()):
        raise CorrectionError('Некорректная пользовательская сессия.', 401)
    request = urllib.request.Request(base + '/api/auth/me', headers=dict(forwarded, Accept='application/json'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=8) as response:
            data = response.read(65537)
            if response.status != 200 or len(data) > 65536:
                raise CorrectionError('Не удалось подтвердить пользователя PM.bi.', 503)
        user = json.loads(data).get('user')
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise CorrectionError('Сессия PM.bi истекла или доступ запрещён.', error.code) from error
        raise CorrectionError('CRM временно недоступна для проверки пользователя.', 503) from error
    except (OSError, ValueError, AttributeError, urllib.error.URLError) as error:
        raise CorrectionError('CRM временно недоступна для проверки пользователя.', 503) from error
    return actor_from_user(user)
