# -*- coding: utf-8 -*-
"""重複通知防止・急落レベル遷移に関するテスト。"""

from common import should_notify


def test_first_crash_notifies():
    notify, level = should_notify(previous_level=0, new_level=1)
    assert notify is True
    assert level == 1


def test_already_notified_same_level_does_not_renotify():
    notify, level = should_notify(previous_level=1, new_level=1)
    assert notify is False
    assert level == 1


def test_level_up_notifies_again():
    notify, level = should_notify(previous_level=1, new_level=2)
    assert notify is True
    assert level == 2


def test_level_down_but_still_crashed_does_not_notify():
    notify, level = should_notify(previous_level=2, new_level=1)
    assert notify is False
    assert level == 2  # 通知済みの最高レベルを維持する


def test_recovery_resets_level():
    notify, level = should_notify(previous_level=1, new_level=0)
    assert notify is False
    assert level == 0


def test_renotify_after_recovery():
    # 一度0まで回復してから再度条件を満たした場合は再通知される
    notify_reset, level_reset = should_notify(previous_level=1, new_level=0)
    assert notify_reset is False
    assert level_reset == 0

    notify_again, level_again = should_notify(previous_level=level_reset, new_level=1)
    assert notify_again is True
    assert level_again == 1


def test_full_scenario_from_spec():
    """
    要件の例をそのままシナリオ化したテスト:
    -2.0% -> 通知, -2.5% -> 通知なし, -1.0% -> 解除, -2.1% -> 再度通知
    """
    level = 0

    notify, level = should_notify(level, new_level=1)  # -2.0%
    assert notify is True and level == 1

    notify, level = should_notify(level, new_level=1)  # -2.5% (同レベル)
    assert notify is False and level == 1

    notify, level = should_notify(level, new_level=0)  # -1.0% (解除)
    assert notify is False and level == 0

    notify, level = should_notify(level, new_level=1)  # -2.1% (再度)
    assert notify is True and level == 1
