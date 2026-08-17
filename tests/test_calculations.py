# -*- coding: utf-8 -*-
"""前日比の計算・急落レベル判定に関するテスト。"""

from decimal import Decimal

import pytest

from common import calc_alert_level, get_level_thresholds, should_notify


@pytest.mark.parametrize(
    "current,prev,expected_pct",
    [
        (105, 100, 0.05),
        (100, 100, 0.0),
        (98.01, 100, -0.0199),
        (98, 100, -0.02),
        (95, 100, -0.05),
        (90, 100, -0.10),
    ],
)
def test_pct_change_calculation(current, prev, expected_pct):
    pct = (current - prev) / prev
    assert round(pct, 4) == round(expected_pct, 4)


def test_level_thresholds_default():
    level1, level2, level3 = get_level_thresholds(Decimal("-2"))
    assert level1 == Decimal("-2")
    assert level2 == Decimal("-5")
    assert level3 == Decimal("-10")


@pytest.mark.parametrize(
    "pct_change,expected_level",
    [
        (0.05, 0),
        (0.0, 0),
        (-0.0199, 0),
        (-0.02, 1),
        (-0.03, 1),
        (-0.05, 2),
        (-0.07, 2),
        (-0.10, 3),
        (-0.20, 3),
    ],
)
def test_calc_alert_level_default_threshold(pct_change, expected_level):
    assert calc_alert_level(pct_change, Decimal("-2")) == expected_level


def test_calc_alert_level_custom_threshold():
    # ユーザーがデフォルトを-3に変更した場合
    assert calc_alert_level(-0.025, Decimal("-3")) == 0
    assert calc_alert_level(-0.03, Decimal("-3")) == 1
    assert calc_alert_level(-0.06, Decimal("-3")) == 2
    assert calc_alert_level(-0.11, Decimal("-3")) == 3
