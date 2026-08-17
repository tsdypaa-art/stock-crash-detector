# -*- coding: utf-8 -*-
"""
lambda_function.py
===================
EventBridge（1分間隔）から呼び出される監視Lambda。

【今回の主な変更点】
1. 重複通知防止: DynamoDBに保存した AlertLevel と比較し、
   レベルが「新しく上昇した」ときだけ通知する（common.should_notify）。
2. 急落レベルの複数段階化: LEVEL1(-2%) / LEVEL2(-5%) / LEVEL3(-10%) を
   ユーザー・銘柄ごとのしきい値から動的に算出する。
3. ユーザー/銘柄ごとのしきい値対応: StockTableの `Threshold` 属性、
   もしくは `#SETTINGS#` 行の `DefaultThreshold` を利用する。
4. 市場時間の考慮: 土日祝・昼休みはAPIアクセスをスキップする
   （common.is_market_open）。EventBridgeは1分ごとに呼び続けて構わない
   （呼び出し自体はLambdaの実行時間・コストにほぼ影響しないが、
   外部APIへの無駄なアクセスと不要なDynamoDB Scanを避ける）。
5. 外部API障害耐性: 銘柄単位でtry/exceptし、1銘柄の失敗が全体を止めない。
6. DynamoDB Scan問題: 現状の規模（個人利用〜小規模）ではScanで十分だが、
   将来ユーザー数が増えた場合に備え、Scanをこの関数に閉じ込めて
   `_fetch_all_watch_items()` として切り出した。ユーザー数が増えてきたら
   ここをGSI(例: レベル別インデックス)+Queryに差し替えるだけで済む構成にしてある。

既存のDynamoDB項目（UserID/Symbol/CompanyNameのみ）はそのまま動作する。
新しい属性は初回実行時に自動的に補完・保存される。
"""

import json
from decimal import Decimal

import boto3

from common import (
    DEFAULT_THRESHOLD,
    SETTINGS_SORT_KEY,
    build_alert_message,
    calc_alert_level,
    get_stock_data_native,
    is_market_open,
    logger,
    now_iso,
    send_line_push,
    should_notify,
)

TABLE_NAME = "StockTable"
REGION = "ap-northeast-1"

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)


def _fetch_all_watch_items():
    """
    StockTableから監視対象の全項目を取得する。
    #SETTINGS# 行（ユーザー設定）は監視対象から除外する。

    現在の想定規模（個人〜小規模の利用者数）ではDynamoDB Scanで十分だが、
    ユーザー数・銘柄数が大きく増える場合は、
    「AlertLevel」等をGSIのパーティションキーにしたQueryへの移行を検討する。
    """
    items = []
    scan_kwargs = {}
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    return [item for item in items if item.get("Symbol") != SETTINGS_SORT_KEY]


def _get_user_settings_cache(user_id, cache):
    """ユーザーごとの #SETTINGS# 行を取得し、リクエスト内でキャッシュする。"""
    if user_id in cache:
        return cache[user_id]

    settings = table.get_item(Key={"UserID": user_id, "Symbol": SETTINGS_SORT_KEY}).get("Item", {})
    cache[user_id] = settings
    return settings


def _resolve_threshold(item, settings):
    """
    銘柄ごとのThreshold > ユーザーのDefaultThreshold > システムデフォルト
    の優先順位でLEVEL1しきい値(%)を決定する。
    """
    if "Threshold" in item and item["Threshold"] is not None:
        return item["Threshold"]
    if "DefaultThreshold" in settings and settings["DefaultThreshold"] is not None:
        return settings["DefaultThreshold"]
    return DEFAULT_THRESHOLD


def lambda_handler(event, context):
    if not is_market_open():
        logger.info("市場時間外のため監視をスキップします")
        return {"statusCode": 200, "body": json.dumps("Market closed")}

    try:
        items = _fetch_all_watch_items()
    except Exception as e:  # noqa: BLE001
        logger.error("DynamoDBからの取得に失敗しました: %s", type(e).__name__)
        return {"statusCode": 500, "body": json.dumps("Failed to fetch items")}

    if not items:
        logger.info("監視対象の銘柄が登録されていません")
        return {"statusCode": 200, "body": json.dumps("No stocks to monitor")}

    settings_cache = {}
    user_notifications = {}
    notify_disabled_users = set()

    for item in items:
        user_id = item.get("UserID")
        symbol = item.get("Symbol")
        if not user_id or not symbol:
            continue

        company_name = item.get("CompanyName", symbol)

        try:
            settings = _get_user_settings_cache(user_id, settings_cache)
        except Exception as e:  # noqa: BLE001
            logger.error("user=%s 設定取得に失敗しました: %s", user_id, type(e).__name__)
            settings = {}

        if settings.get("NotifyEnabled") is False:
            notify_disabled_users.add(user_id)
            continue

        try:
            current_price, prev_close = get_stock_data_native(symbol)
        except Exception as e:  # noqa: BLE001
            # 1銘柄の予期しない例外でシステム全体を止めない
            logger.error("symbol=%s 予期しないエラーで取得をスキップします: %s", symbol, type(e).__name__)
            continue

        if current_price is None or prev_close is None:
            logger.warning("symbol=%s データ取得失敗のためスキップします", symbol)
            continue

        pct_change = (current_price - prev_close) / prev_close
        threshold = _resolve_threshold(item, settings)
        new_level = calc_alert_level(pct_change, threshold)
        previous_level = int(item.get("AlertLevel", 0) or 0)

        notify, level_to_store = should_notify(previous_level, new_level)

        # レベルに変化があった場合のみDynamoDBを更新（無駄な書き込みを減らす）
        if level_to_store != previous_level:
            try:
                table.update_item(
                    Key={"UserID": user_id, "Symbol": symbol},
                    UpdateExpression=(
                        "SET AlertLevel = :lvl, LastPctChange = :pct, "
                        "LastAlertTime = :t, UpdatedAt = :t"
                    ),
                    ExpressionAttributeValues={
                        ":lvl": level_to_store,
                        ":pct": Decimal(str(round(pct_change * 100, 4))),
                        ":t": now_iso(),
                    },
                )
            except Exception as e:  # noqa: BLE001
                logger.error("symbol=%s DynamoDB更新に失敗しました: %s", symbol, type(e).__name__)

        if notify:
            message = build_alert_message(
                company_name, symbol, pct_change, current_price, prev_close, level_to_store
            )
            user_notifications.setdefault(user_id, []).append(message)
            logger.info("symbol=%s 急落を検知しました level=%d change=%.2f%%", symbol, level_to_store, pct_change * 100)

    for user_id, messages in user_notifications.items():
        final_message = "\n\n".join(messages)
        send_line_push(user_id, final_message)

    return {"statusCode": 200, "body": json.dumps("Success")}
