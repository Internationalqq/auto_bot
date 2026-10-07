"""Anya-only Telegram DM admission for verified finance-group members.

No poller, messages, persistent pairing grants, or credentials are created.
The gateway's existing auth gate consumes the process-local allowlist. Cold
DM turns verify membership again; busy turns retain that session admission.
"""
import json
import logging
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request

PROFILE = Path('/Users/egor/.hermes/profiles/anya')
GROUP = '-5589110678'
OWNER = '1228838420'
logger = logging.getLogger(__name__)


def lookup_member(user_id):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + token + '/getChatMember',
        data=urllib.parse.urlencode({'chat_id': GROUP, 'user_id': user_id}).encode(),
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        payload = json.load(response)
    if payload.get('ok') is not True:
        raise ValueError('membership check unsuccessful')
    return payload['result']


def is_member(member, user_id):
    user = member.get('user', {})
    if str(user.get('id')) != user_id or user.get('is_bot') is not False:
        return False
    return member.get('status') in {'creator', 'administrator', 'member'} or (
        member.get('status') == 'restricted' and member.get('is_member') is True
    )


def record(user_id, member):
    path = PROFILE / 'workspace/group-finance/register/dm-access.jsonl'
    entry = {'at': time.time(), 'group_id': GROUP, 'user_id': user_id,
             'status': member.get('status'), 'dm_received': True}
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry) + '\n')


def admit_dm(event=None, **kwargs):
    if Path(os.environ.get('HERMES_HOME', '')) != PROFILE:
        return None
    source = getattr(event, 'source', None)
    if (getattr(getattr(source, 'platform', None), 'value', None) != 'telegram'
            or getattr(source, 'chat_type', None) != 'dm'):
        return None
    user_id = str(getattr(source, 'user_id', '') or '')
    if user_id == OWNER:
        return None
    allowed = {x.strip() for x in os.environ.get('TELEGRAM_ALLOWED_USERS', '').split(',') if x.strip()}
    # Remove the previous turn's grant before attempting a fresh verification.
    allowed.discard(user_id)
    os.environ['TELEGRAM_ALLOWED_USERS'] = ','.join(sorted(allowed))
    if not user_id.isdecimal() or str(source.chat_id) != user_id:
        return {'action': 'skip', 'reason': 'finance_dm_identity_unverified'}
    try:
        member = lookup_member(user_id)
        if not is_member(member, user_id):
            return {'action': 'skip', 'reason': 'finance_dm_not_a_group_member'}
        record(user_id, member)
    except Exception as exc:
        # Never log exception text: HTTP exceptions can contain the bot token.
        logger.warning('Finance DM membership unavailable: %s', type(exc).__name__)
        return {'action': 'skip', 'reason': 'finance_dm_membership_unavailable'}
    allowed.add(user_id)
    os.environ['TELEGRAM_ALLOWED_USERS'] = ','.join(sorted(allowed))
    return None


def register(ctx):
    if Path(os.environ.get('HERMES_HOME', '')) == PROFILE:
        ctx.register_hook('pre_gateway_dispatch', admit_dm)
