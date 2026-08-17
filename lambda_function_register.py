# -*- coding: utf-8 -*-
"""
lambda_function_register.py
============================
API Gateway経由でLINEからのWebhookを受け取るLambda。

【今回の主な変更点】
1. コマンド体系の拡張: 追加 / 削除 / 一覧 / ヘルプ / 設定
2. 「追加 7203.T」だけでも登録可能に（会社名はYahoo Financeから自動取得、
   取得できない場合は銘柄コードをそのまま表示名としてフォールバック）
3. 登録前に銘柄の存在確認を行い、存在しない銘柄コードを「登録しました」と
   誤認させないようにした（確認不能な場合は登録を許可しつつ注意書きを添える）
4. 入力ミス時のガイダンスを強化（何が間違っているかを具体的に案内）
5. 「設定」コマンドでユーザーごとのデフォルトしきい値・銘柄別しきい値・
   通知ON/OFFを確認・変更できるようにした
6. 「一覧」で現在値・前日比・監視しきい値を表示するよう高機能化
7. DynamoDBの操作（追加/削除/更新/参照）で例外を個別にtry/exceptし、
   1件の失敗が全体の応答を止めないようにした

DynamoDBのキー構造（UserID PK / Symbol SK）はそのまま維持しており、
既存データとの後方互換性がある。ユーザー設定は同テーブルの
Symbol="#SETTINGS#" 行に保存する（common.py参照）。
"""

import json
from decimal import Decimal, InvalidOperation

import boto3
from boto3.dynamodb.conditions import Key

from common import (
    DEFAULT_THRESHOLD,
    SETTINGS_SORT_KEY,
    get_company_name,
    get_level_thresholds,
    get_stock_data_native,
    logger,
    now_iso,
    send_line_reply,
    symbol_exists,
)

TABLE_NAME = "StockTable"
REGION = "ap-northeast-1"

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)

HELP_TEXT = (
    "【Stock Crash Detector】\n"
    "利用可能なコマンド\n\n"
    "📌 銘柄追加\n"
    "追加 7203.T\n"
    "追加 7203.T トヨタ\n\n"
    "📌 銘柄削除\n"
    "削除 7203.T\n\n"
    "📌 監視一覧\n"
    "一覧\n\n"
    "📌 設定確認\n"
    "設定\n\n"
    "📌 デフォルトしきい値の変更\n"
    "設定 -3\n\n"
    "📌 銘柄ごとのしきい値の変更\n"
    "設定 7203.T -5\n\n"
    "📌 ヘルプ\n"
    "ヘルプ"
)

USAGE_TEXT = (
    "❌ 入力形式が正しくありません。\n\n"
    "【銘柄を追加する】\n"
    "「追加 銘柄コード」または「追加 銘柄コード 会社名」\n"
    "例）追加 7203.T\n\n"
    "【銘柄を削除する】\n"
    "「削除 銘柄コード」\n"
    "例）削除 7203.T\n\n"
    "わからないときは「ヘルプ」と送ってください。"
)


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body", "{}"))
    except (json.JSONDecodeError, TypeError):
        logger.error("リクエストボディのJSON解析に失敗しました")
        return {"statusCode": 400, "body": json.dumps("Invalid body")}

    events = body.get("events", [])
    if not events:
        return {"statusCode": 200, "body": json.dumps("No events")}

    for line_event in events:
        try:
            _handle_single_event(line_event)
        except Exception as e:  # noqa: BLE001
            # 1件のイベント処理失敗が他のイベントに波及しないようにする
            logger.error("イベント処理中にエラーが発生しました: %s", type(e).__name__)

    return {"statusCode": 200, "body": json.dumps("Success")}


def _handle_single_event(line_event):
    if line_event.get("type") != "message":
        return

    message = line_event.get("message", {})
    if message.get("type") != "text":
        return

    user_text = message.get("text", "").strip()
    reply_token = line_event.get("replyToken")
    user_id = line_event.get("source", {}).get("userId", "unknown_user")

    words = user_text.split()
    if not words:
        send_line_reply(reply_token, USAGE_TEXT)
        return

    command = words[0]

    if command == "ヘルプ":
        reply_text = HELP_TEXT
    elif command == "追加":
        reply_text = _handle_add(user_id, words)
    elif command == "削除":
        reply_text = _handle_delete(user_id, words)
    elif command == "一覧":
        reply_text = _handle_list(user_id)
    elif command == "設定":
        reply_text = _handle_settings(user_id, words)
    else:
        reply_text = USAGE_TEXT

    send_line_reply(reply_token, reply_text)


# ---------------------------------------------------------------------------
# 追加
# ---------------------------------------------------------------------------

def _handle_add(user_id, words):
    if len(words) not in (2, 3):
        return (
            "❌ 銘柄を追加するには、\n"
            "「追加 銘柄コード」または「追加 銘柄コード 会社名」\n"
            "と入力してください。\n\n"
            "例）\n追加 7203.T\n追加 7203.T トヨタ"
        )

    symbol = words[1]
    company_name = words[2] if len(words) == 3 else None

    exists = symbol_exists(symbol)
    if exists is False:
        return (
            f"❌ 銘柄コード「{symbol}」が見つかりませんでした。\n"
            "銘柄コードが正しいかご確認のうえ、再度お試しください。\n"
            "（例：トヨタ自動車 → 7203.T）"
        )

    warning = ""
    if exists is None:
        warning = "\n⚠️ 現在、銘柄情報の確認ができなかったため、登録のみ行いました。次回の監視時にあらためて確認します。"

    if company_name is None:
        company_name = get_company_name(symbol) or symbol

    try:
        table.put_item(
            Item={
                "UserID": user_id,
                "Symbol": symbol,
                "CompanyName": company_name,
                "AlertLevel": 0,
                "CreatedAt": now_iso(),
                "UpdatedAt": now_iso(),
            }
        )
    except Exception as e:  # noqa: BLE001
        logger.error("symbol=%s DynamoDBへの登録に失敗しました: %s", symbol, type(e).__name__)
        return "⚠️ 登録処理中にエラーが発生しました。時間をおいて再度お試しください。"

    return (
        "✅ 登録完了しました！\n"
        f"銘柄: {symbol}\n"
        f"企業名: {company_name}\n"
        "次回の監視から自動監視を開始します。" + warning
    )


# ---------------------------------------------------------------------------
# 削除
# ---------------------------------------------------------------------------

def _handle_delete(user_id, words):
    if len(words) != 2:
        return "❌ 銘柄を削除するには、「削除 銘柄コード」と入力してください。\n例）削除 7203.T"

    symbol = words[1]
    try:
        existing = table.get_item(Key={"UserID": user_id, "Symbol": symbol}).get("Item")
    except Exception as e:  # noqa: BLE001
        logger.error("symbol=%s 削除前の確認に失敗しました: %s", symbol, type(e).__name__)
        return "⚠️ 削除処理中にエラーが発生しました。時間をおいて再度お試しください。"

    if not existing:
        return f"⚠️ 銘柄「{symbol}」は監視リストに登録されていません。\n「一覧」で現在の監視銘柄を確認できます。"

    try:
        table.delete_item(Key={"UserID": user_id, "Symbol": symbol})
    except Exception as e:  # noqa: BLE001
        logger.error("symbol=%s 削除に失敗しました: %s", symbol, type(e).__name__)
        return "⚠️ 削除処理中にエラーが発生しました。時間をおいて再度お試しください。"

    return f"🗑️ 削除完了しました！\n銘柄: {symbol}\nこの銘柄の自動監視を停止しました。"


# ---------------------------------------------------------------------------
# 一覧
# ---------------------------------------------------------------------------

def _handle_list(user_id):
    try:
        response = table.query(KeyConditionExpression=Key("UserID").eq(user_id))
        items = [i for i in response.get("Items", []) if i.get("Symbol") != SETTINGS_SORT_KEY]
    except Exception as e:  # noqa: BLE001
        logger.error("user=%s 一覧取得に失敗しました: %s", user_id, type(e).__name__)
        return "⚠️ 一覧の取得中にエラーが発生しました。時間をおいて再度お試しください。"

    if not items:
        return "📊 監視中の銘柄は現在ありません。\n「追加 銘柄コード」で登録してください。"

    settings = table.get_item(Key={"UserID": user_id, "Symbol": SETTINGS_SORT_KEY}).get("Item", {})
    default_threshold = settings.get("DefaultThreshold", DEFAULT_THRESHOLD)

    lines = ["📊 監視銘柄\n"]
    for idx, item in enumerate(items, start=1):
        symbol = item["Symbol"]
        company_name = item.get("CompanyName", symbol)
        threshold = item.get("Threshold", default_threshold)

        current_price, prev_close = get_stock_data_native(symbol, retries=0)
        lines.append(f"{idx}️⃣ {company_name}\n{symbol}")
        if current_price is None or prev_close is None:
            lines.append("⚠️ データ取得エラー")
        else:
            pct = (current_price - prev_close) / prev_close * 100
            lines.append(f"現在値：{current_price:,.0f}円")
            lines.append(f"前日比：{pct:.2f}%")
        lines.append(f"監視条件：{threshold}%\n")

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

def _handle_settings(user_id, words):
    if len(words) == 1:
        return _show_settings(user_id)

    if len(words) == 2:
        return _set_default_threshold(user_id, words[1])

    if len(words) == 3:
        return _set_symbol_threshold(user_id, words[1], words[2])

    return (
        "❌ 設定コマンドの形式が正しくありません。\n\n"
        "【デフォルトしきい値の変更】\n設定 -3\n\n"
        "【銘柄ごとのしきい値の変更】\n設定 7203.T -5"
    )


def _parse_threshold(text):
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if value >= 0 or value < -50:
        return None
    return value


def _show_settings(user_id):
    settings = table.get_item(Key={"UserID": user_id, "Symbol": SETTINGS_SORT_KEY}).get("Item", {})
    default_threshold = settings.get("DefaultThreshold", DEFAULT_THRESHOLD)
    notify_enabled = settings.get("NotifyEnabled", True)

    try:
        response = table.query(KeyConditionExpression=Key("UserID").eq(user_id))
        watch_count = len([i for i in response.get("Items", []) if i.get("Symbol") != SETTINGS_SORT_KEY])
    except Exception:  # noqa: BLE001
        watch_count = "取得失敗"

    level1, level2, level3 = get_level_thresholds(default_threshold)

    return (
        "⚙️ 現在の設定\n\n"
        f"デフォルト急落しきい値：{default_threshold}%\n\n"
        f"通知：{'ON' if notify_enabled else 'OFF'}\n\n"
        f"監視銘柄：{watch_count}銘柄\n\n"
        "警戒レベル：\n"
        f"LEVEL1：{level1}%\n"
        f"LEVEL2：{level2}%\n"
        f"LEVEL3：{level3}%\n\n"
        "デフォルトを変えるには「設定 -3」のように、\n"
        "銘柄ごとに変えるには「設定 7203.T -5」のように送ってください。"
    )


def _set_default_threshold(user_id, threshold_text):
    threshold = _parse_threshold(threshold_text)
    if threshold is None:
        return (
            "❌ しきい値の形式が正しくありません。\n"
            "0未満・-50以上のマイナスの数値で指定してください。\n"
            "例）設定 -3"
        )

    try:
        table.put_item(
            Item={
                "UserID": user_id,
                "Symbol": SETTINGS_SORT_KEY,
                "DefaultThreshold": threshold,
                "NotifyEnabled": True,
                "UpdatedAt": now_iso(),
            }
        )
    except Exception as e:  # noqa: BLE001
        logger.error("user=%s 設定更新に失敗しました: %s", user_id, type(e).__name__)
        return "⚠️ 設定の更新中にエラーが発生しました。時間をおいて再度お試しください。"

    return f"✅ デフォルトの急落しきい値を {threshold}% に変更しました。"


def _set_symbol_threshold(user_id, symbol, threshold_text):
    threshold = _parse_threshold(threshold_text)
    if threshold is None:
        return (
            "❌ しきい値の形式が正しくありません。\n"
            "0未満・-50以上のマイナスの数値で指定してください。\n"
            "例）設定 7203.T -5"
        )

    try:
        existing = table.get_item(Key={"UserID": user_id, "Symbol": symbol}).get("Item")
    except Exception as e:  # noqa: BLE001
        logger.error("symbol=%s 設定変更前の確認に失敗しました: %s", symbol, type(e).__name__)
        return "⚠️ 設定の更新中にエラーが発生しました。時間をおいて再度お試しください。"

    if not existing:
        return f"⚠️ 銘柄「{symbol}」は監視リストに登録されていません。\nまず「追加 {symbol}」で登録してください。"

    try:
        table.update_item(
            Key={"UserID": user_id, "Symbol": symbol},
            UpdateExpression="SET Threshold = :th, UpdatedAt = :t",
            ExpressionAttributeValues={":th": threshold, ":t": now_iso()},
        )
    except Exception as e:  # noqa: BLE001
        logger.error("symbol=%s 設定更新に失敗しました: %s", symbol, type(e).__name__)
        return "⚠️ 設定の更新中にエラーが発生しました。時間をおいて再度お試しください。"

    return f"✅ {symbol} の急落しきい値を {threshold}% に変更しました。"
