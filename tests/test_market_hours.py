# -*- coding: utf-8 -*-
"""市場時間（土日祝・昼休み）判定のテスト。"""

from datetime import datetime

from common import JST, is_market_open


def _jst(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=JST)


def test_weekday_morning_session_is_open():
    # 2026-08-17は月曜日
    assert is_market_open(_jst(2026, 8, 17, 10, 0)) is True


def test_weekday_afternoon_session_is_open():
    assert is_market_open(_jst(2026, 8, 17, 13, 0)) is True


def test_lunch_break_is_closed():
    assert is_market_open(_jst(2026, 8, 17, 12, 0)) is False


def test_before_open_is_closed():
    assert is_market_open(_jst(2026, 8, 17, 8, 30)) is False


def test_after_close_is_closed():
    assert is_market_open(_jst(2026, 8, 17, 15, 30)) is False


def test_saturday_is_closed():
    assert is_market_open(_jst(2026, 8, 22, 10, 0)) is False


def test_holiday_is_closed():
    # 2026-01-01 元日
    assert is_market_open(_jst(2026, 1, 1, 10, 0)) is False
