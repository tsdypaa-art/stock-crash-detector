# -*- coding: utf-8 -*-
"""
common.py
=========
lambda_function.py（監視Lambda）と lambda_function_register.py（LINE Webhook Lambda）
の両方から利用する共通処理をまとめたモジュール。

既存のAWS構成（Lambda 2関数 + DynamoDB 1テーブル）をそのまま活かしつつ、
コードの重複と責務の混在を減らすために切り出した。

【後方互換性についての方針】
既存のDynamoDB「StockTable」の項目は
  UserID (PK) / Symbol (SK) / CompanyName
のみだった。今回、以下の属性を追加するが、既存データには存在しないため
すべて `.get(key, デフォルト値)` で安全に読み出す。既存データが壊れることはない。

  Threshold        : 銘柄ごとの急落しきい値（%）。未設定ならユーザーのデフォルトを使う
  AlertLevel       : 現在の警戒レベル（0-3）。重複通知防止に使う
  LastAlertPct     : 直近の通知時点での前日比（デバッグ用）
  LastAlertTime    : 直近の通知日時（ISO8601）
  CreatedAt/UpdatedAt

また、ユーザーごとの設定（デフォルトしきい値・通知ON/OFF）は新しいテーブルを
増やさず、同じ StockTable に
  UserID = <ユーザーID> / Symbol = "#SETTINGS#"
という特別な行として保存する（銘柄コードが "#SETTINGS#" になることは無いため
既存の監視銘柄と衝突しない）。これにより既存のテーブル1本構成・既存のGSI無し
という低コスト構成を維持できる。
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

JST = timezone(timedelta(hours=9))

SETTINGS_SORT_KEY = "#SETTINGS#"

DEFAULT_THRESHOLD = Decimal("-2")   # LEVEL1のデフォルト（前日比 -2%）
LEVEL2_OFFSET = Decimal("-3")       # LEVEL1しきい値からさらに-3%でLEVEL2
LEVEL3_OFFSET = Decimal("-8")       # LEVEL1しきい値からさらに-8%でLEVEL3

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

# ---------------------------------------------------------------------------
# 株価取得（Yahoo Finance / yfinance非依存）
# ---------------------------------------------------------------------------

def get_stock_data_native(symbol, retries=2, timeout=5):
    """
    Yahoo FinanceのチャートAPIから現在値と前日終値を取得する。

    外部API障害を考慮し、
    - タイムアウト
    - HTTPエラー
    - レスポンス構造異常
    - 値がNone/異常
    をすべて捕捉し、失敗時は (None, None) を返す（呼び出し側はスキップする設計）。
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    last_error = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode())

            chart = data.get("chart", {})
            if chart.get("error"):
                logger.warning("symbol=%s Yahoo Financeがエラーを返しました: %s", symbol, chart["error"])
                return None, None

            result_list = chart.get("result")
            if not result_list:
                logger.warning("symbol=%s resultが空です", symbol)
                return None, None

            result = result_list[0]
            meta = result.get("meta", {})
            quote_list = result.get("indicators", {}).get("quote", [{}])
            closes = quote_list[0].get("close") if quote_list else None

            current_price = None
            if closes:
                # Noneでない最新の値を後ろから探す（市場直後の欠損対策）
                for value in reversed(closes):
                    if value is not None:
                        current_price = value
                        break

            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")

            if current_price is None or prev_close is None:
                logger.warning("symbol=%s 現在値または前日終値がNoneです", symbol)
                return None, None

            if current_price <= 0 or prev_close <= 0:
                logger.warning("symbol=%s 異常な価格を検出しました current=%s prev=%s", symbol, current_price, prev_close)
                return None, None

            return float(current_price), float(prev_close)

        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as e:
            last_error = e
            logger.warning("symbol=%s データ取得試行%d回目失敗: %s", symbol, attempt + 1, e)
            time.sleep(0.3)
            continue

    logger.error("symbol=%s データ取得に最終的に失敗しました: %s", symbol, last_error)
    return None, None


def get_company_name(symbol, timeout=5):
    """
    Yahoo Financeのquoteエンドポイントから会社名（略称）を取得する。
    取得できない場合はNoneを返す（呼び出し側で銘柄コードにフォールバックする）。
    """
    url = f"https://query1.finance.yahoo.com/v6/finance/quote?symbols={symbol}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode())
        results = data.get("quoteResponse", {}).get("result", [])
        if not results:
            return None
        info = results[0]
        return info.get("shortName") or info.get("longName")
    except Exception as e:  # noqa: BLE001 - 会社名取得は失敗しても致命的ではない
        logger.warning("symbol=%s 会社名の自動取得に失敗しました: %s", symbol, e)
        return None


def symbol_exists(symbol, timeout=5):
    """
    銘柄コードが実在するかどうかを確認する。
    Yahoo Finance側の障害時は「存在しない」と誤判定して登録をブロックしないよう、
    確認できなかった場合は True（登録を許可）を返すフォールバック方針とする。
    """
    price, prev_close = get_stock_data_native(symbol, retries=1)
    if price is not None and prev_close is not None:
        return True
    # 取得失敗＝存在しないとは限らないため、名称取得も試す
    name = get_company_name(symbol, timeout=timeout)
    if name:
        return True
    return None  # 判定不能（呼び出し側で警告を出しつつ登録は許可する）


# ---------------------------------------------------------------------------
# 市場時間の判定（日本株）
# ---------------------------------------------------------------------------

# 2026年 東京証券取引所 休場日（年末年始・祝日）。運用時は毎年更新するか、
# 外部の取引カレンダーAPI/ライブラリ（jpholiday等）に置き換えることを推奨。
JP_MARKET_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-01-12", "2026-02-11", "2026-02-23",
    "2026-03-20", "2026-04-29", "2026-05-04",
    "2026-05-05", "2026-05-06", "2026-07-20",
    "2026-08-11", "2026-09-21", "2026-09-22",
    "2026-09-23", "2026-10-12", "2026-11-03",
    "2026-11-23", "2026-12-31",
}


def is_market_open(now_jst=None):
    """
    日本株市場が「今」取引時間中かどうかを判定する。
    - 土日は休場
    - 上記の祝日・年末年始は休場
    - 前場 9:00-11:30 / 昼休み 11:30-12:30 / 後場 12:30-15:00
    を考慮する。無意味なAPIアクセスを減らすために監視Lambda側で使用する。
    """
    now = now_jst or datetime.now(JST)

    if now.weekday() >= 5:  # 5=土, 6=日
        return False

    if now.strftime("%Y-%m-%d") in JP_MARKET_HOLIDAYS_2026:
        return False

    t = now.time()
    morning = (t.hour, t.minute) >= (9, 0) and (t.hour, t.minute) < (11, 30)
    afternoon = (t.hour, t.minute) >= (12, 30) and (t.hour, t.minute) < (15, 0)
    return morning or afternoon


# ---------------------------------------------------------------------------
# 急落レベル判定・重複通知防止
# ---------------------------------------------------------------------------

def get_level_thresholds(level1_threshold):
    """
    ユーザー/銘柄ごとのLEVEL1しきい値から、LEVEL2・LEVEL3のしきい値を算出する。
    例: level1=-2 → level2=-5, level3=-10 (元仕様のデフォルトと一致)
    """
    level1 = Decimal(str(level1_threshold))
    level2 = level1 + LEVEL2_OFFSET
    level3 = level1 + LEVEL3_OFFSET
    return level1, level2, level3


def calc_alert_level(pct_change, level1_threshold):
    """
    前日比(pct_change, 例: -0.0523 = -5.23%)から警戒レベル(0-3)を算出する。
    level1_threshold は %表記（例: Decimal("-2")）。
    """
    pct_percent = Decimal(str(pct_change)) * Decimal("100")
    level1, level2, level3 = get_level_thresholds(level1_threshold)

    if pct_percent <= level3:
        return 3
    if pct_percent <= level2:
        return 2
    if pct_percent <= level1:
        return 1
    return 0


def should_notify(previous_level, new_level):
    """
    重複通知防止のコアロジック。
    - レベルが「上昇」した場合のみ通知する（同レベル維持・レベル低下では通知しない）
    - 一度0まで戻ってから再度条件を満たした場合は再通知される
    戻り値: (通知するか: bool, 保存すべき新しいAlertLevel: int)
    """
    if new_level > previous_level:
        return True, new_level
    if new_level == 0:
        return False, 0
    # レベルは上がっていないが、まだ急落状態が継続中 → 通知せず、レベルは維持
    return False, previous_level


LEVEL_LABELS = {
    0: "通常",
    1: "急落注意",
    2: "急落警戒",
    3: "暴落",
}


# ---------------------------------------------------------------------------
# 通知メッセージ生成
# ---------------------------------------------------------------------------

def build_alert_message(company_name, symbol, pct_change, current_price, prev_close, level):
    change_percent = pct_change * 100
    now_str = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    label = LEVEL_LABELS.get(level, "急落注意")
    return (
        "🚨 株価急落アラート\n"
        "━━━━━━━━━━\n"
        f"🏢 {company_name}\n"
        f"{symbol}\n"
        "━━━━━━━━━━\n\n"
        "📉 前日比\n"
        f"{change_percent:.2f}%\n\n"
        "💰 現在値\n"
        f"{current_price:,.2f}円\n\n"
        "📌 前日終値\n"
        f"{prev_close:,.2f}円\n\n"
        f"⚠️ 警戒レベル\n"
        f"LEVEL {level}（{label}）\n\n"
        "🕐 検知時刻\n"
        f"{now_str}\n\n"
        "※本通知は株価の変動を機械的に検知したものであり、\n"
        "売買を推奨するものではありません。投資判断はご自身で行ってください。"
    )


# ---------------------------------------------------------------------------
# LINE送受信
# ---------------------------------------------------------------------------

def _line_post(url, payload, timeout=5):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        logger.error("環境変数 LINE_CHANNEL_ACCESS_TOKEN が設定されていません")
        return False

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError as e:
        # レスポンスボディにユーザーIDや個人情報が含まれる場合があるため詳細はログに出さない
        logger.error("LINE APIエラー status=%s", e.code)
        return False
    except Exception as e:  # noqa: BLE001
        logger.error("LINE送信中に予期しないエラー: %s", type(e).__name__)
        return False


def send_line_push(user_id, text):
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    ok = _line_post(LINE_PUSH_URL, payload)
    if ok:
        logger.info("LINEプッシュ通知を送信しました")
    return ok


def send_line_reply(reply_token, text):
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    ok = _line_post(LINE_REPLY_URL, payload)
    if ok:
        logger.info("LINE返信を送信しました")
    return ok


def now_iso():
    return datetime.now(JST).isoformat()
