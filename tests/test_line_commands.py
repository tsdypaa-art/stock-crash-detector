# -*- coding: utf-8 -*-
"""
LINEコマンド処理のテスト。

DynamoDB(boto3)・LINE API呼び出しはモックし、外部通信を発生させない。
実行にはboto3が必要（本番Lambda環境・CI環境では requirements.txt でインストールされる）。
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

boto3 = pytest.importorskip("boto3")

import lambda_function_register as reg  # noqa: E402


@pytest.fixture(autouse=True)
def mock_table():
    with patch.object(reg, "table") as mock_table:
        yield mock_table


@pytest.fixture(autouse=True)
def mock_line_reply():
    with patch.object(reg, "send_line_reply") as mock_reply:
        yield mock_reply


def _make_event(text, user_id="U123", reply_token="token123"):
    return {
        "type": "message",
        "replyToken": reply_token,
        "source": {"userId": user_id},
        "message": {"type": "text", "text": text},
    }


def test_help_command(mock_line_reply):
    reg._handle_single_event(_make_event("ヘルプ"))
    args, _ = mock_line_reply.call_args
    assert "利用可能なコマンド" in args[1]


def test_invalid_command_shows_usage(mock_line_reply):
    reg._handle_single_event(_make_event("こんにちは"))
    args, _ = mock_line_reply.call_args
    assert "入力形式が正しくありません" in args[1]


def test_add_without_symbol_shows_guidance(mock_line_reply):
    reg._handle_single_event(_make_event("追加"))
    args, _ = mock_line_reply.call_args
    assert "追加 銘柄コード" in args[1]


@patch.object(reg, "symbol_exists", return_value=True)
@patch.object(reg, "get_company_name", return_value="トヨタ自動車")
def test_add_valid_symbol_without_company_name(mock_name, mock_exists, mock_table, mock_line_reply):
    reg._handle_single_event(_make_event("追加 7203.T"))
    mock_table.put_item.assert_called_once()
    args, _ = mock_line_reply.call_args
    assert "登録完了" in args[1]
    assert "トヨタ自動車" in args[1]


@patch.object(reg, "symbol_exists", return_value=False)
def test_add_nonexistent_symbol_is_rejected(mock_exists, mock_table, mock_line_reply):
    reg._handle_single_event(_make_event("追加 XXXX.T"))
    mock_table.put_item.assert_not_called()
    args, _ = mock_line_reply.call_args
    assert "見つかりませんでした" in args[1]


def test_delete_nonexistent_symbol(mock_table, mock_line_reply):
    mock_table.get_item.return_value = {}
    reg._handle_single_event(_make_event("削除 7203.T"))
    mock_table.delete_item.assert_not_called()
    args, _ = mock_line_reply.call_args
    assert "登録されていません" in args[1]


def test_delete_existing_symbol(mock_table, mock_line_reply):
    mock_table.get_item.return_value = {"Item": {"Symbol": "7203.T"}}
    reg._handle_single_event(_make_event("削除 7203.T"))
    mock_table.delete_item.assert_called_once()
    args, _ = mock_line_reply.call_args
    assert "削除完了" in args[1]


def test_list_empty(mock_table, mock_line_reply):
    mock_table.query.return_value = {"Items": []}
    reg._handle_single_event(_make_event("一覧"))
    args, _ = mock_line_reply.call_args
    assert "現在ありません" in args[1]


@pytest.mark.parametrize(
    "input_text,expected",
    [
        ("-3", Decimal("-3")),
        ("-0.5", Decimal("-0.5")),
        ("3", None),        # 正の数はNG
        ("-60", None),      # 範囲外はNG
        ("abc", None),      # 数値でないためNG
    ],
)
def test_parse_threshold(input_text, expected):
    assert reg._parse_threshold(input_text) == expected


def test_settings_default_threshold_update(mock_table, mock_line_reply):
    reg._handle_single_event(_make_event("設定 -3"))
    mock_table.put_item.assert_called_once()
    args, _ = mock_line_reply.call_args
    assert "-3" in args[1]


def test_settings_symbol_threshold_requires_existing_symbol(mock_table, mock_line_reply):
    mock_table.get_item.return_value = {}
    reg._handle_single_event(_make_event("設定 7203.T -5"))
    mock_table.update_item.assert_not_called()
    args, _ = mock_line_reply.call_args
    assert "登録されていません" in args[1]
