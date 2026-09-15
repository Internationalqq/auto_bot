"""Business date evaluated when the operation runs, using the CRM UTC offset."""
import os
from datetime import datetime, timedelta, timezone


def today_iso():
    offset = int(os.environ.get('PMBI_TZ_OFFSET_HOURS', '5'))
    return datetime.now(timezone(timedelta(hours=offset))).date().isoformat()
