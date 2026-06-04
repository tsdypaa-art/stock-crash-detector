import os
import urllib.request
import json

def lambda_handler(event, context):
    # 監視したい銘柄コード（東証は末尾に .T をつける）
    symbol = "6331.T"
    
    # Yahoo Financeから株価データを取得するためのURL（APIエンドポイント）
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    
    # 一般的なブラウザからのアクセスに見せかけるための設定（ブロック対策）
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    
    try:
        # インターネット経由でYahoo Financeにデータをリクエスト
        with urllib.request.urlopen(req) as response:
            # 返ってきた生データ（JSON形式）をPythonの辞書型に変換
            data = json.loads(response.read().decode())
            result = data["chart"]["result"][0]
            
            # 必要なデータ（現在値と前日の終値）をピンポイントで抽出
            current_price = result["meta"]["regularMarketPrice"]     
            previous_close = result["meta"]["previousClose"]   
            
            # 前日終値からの変動率（前日比）を計算
            price_change_rate = (current_price - previous_close) / previous_close
            
            # クラウド（AWS）のログに、現在の株価情報を出力（デバッグ用）
            print(f"銘柄: {symbol}, 前日終値: {previous_close}, 現在値: {current_price}, 変動率: {price_change_rate:.2%}")
            
            # 🚨 暴落と判定する基準値（-2%以下になったら通知する設定）
            CRASH_THRESHOLD = -0.02
            
            # 計算した変動率が、基準値（-2%）よりも低くなっているか判定
            if price_change_rate <= CRASH_THRESHOLD:
                # 基準を超えていたら、下の「LINE通知関数」を呼び出す
                send_notification(symbol, current_price, price_change_rate)
                return "判定条件を満たしたため、通知処理を呼び出しました。"
                
            return "価格は正常範囲内です。"
            
    except Exception as e:
        # 途中で通信エラーなどが起きた場合は、エラー内容をログに残してプログラムを終了
        print(f"エラーが発生しました: {e}")
        raise e

def send_notification(symbol, price, change_rate):
    # AWSの「環境変数」という隠し金庫から、LINEのアクセストークン（鍵）を取り出す
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        print("LINEのアクセストークンが設定されていないため、送信をスキップします。")
        return

    # LINEに送信するメッセージの本文を組み立てる
    message_text = f"🚨【株価アラート】🚨\n銘柄: {symbol}\n現在値: ¥{price}\n前日比: {change_rate:.2%}\n基準値を超えたため通知します。"

    # LINE Messaging API（公式アカウントからメッセージを全員に一斉送信する窓口）のURL
    url = "https://api.line.me/v2/bot/message/broadcast"
    
    # LINEのサーバーに「私は本物の管理者です」と証明するための認証ヘッダー
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    # LINEに送りつけるデータの中身（メッセージの種類と本文）
    data = {
        "messages": [
            {
                "type": "text",
                "text": message_text
            }
        ]
    }
    
    # データを機械が読める形式（JSON/UTF-8）にエンコードして、POSTメソッドでLINEに送信リクエスト
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as res:
            # LINE側が正常に受け取ったらログに出力（200番が返ってくれば成功）
            print(f"LINE通知が正常に送信されました。ステータスコード: {res.getcode()}")
    except Exception as e:
        # LINEへの送信中にエラーが起きた場合のログ出力
        print(f"LINE通知の送信中にエラーが発生しました: {e}")
