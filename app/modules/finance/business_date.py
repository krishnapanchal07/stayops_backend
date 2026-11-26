from datetime import datetime
from zoneinfo import ZoneInfo


BUSINESS_TIMEZONE = ZoneInfo('Asia/Kolkata')


def business_today():
    return datetime.now(BUSINESS_TIMEZONE).date()
