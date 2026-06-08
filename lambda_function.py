import os
import urllib.request
import json
import boto3
import yfinance as yf
from datetime import datetime, timedelta

def lambda_handler(event, context):
    # 東京リージョンのDynamoDBに接続
    dynamodb = boto3.resource('dynamodb', region_name='ap-northeast-1')
    table = dynamodb.Table('StockTable')
    
    try:
        # DynamoDBから全データを取得
        response = table.scan()
        items = response.get('Items', [])
        
        if not items:
            print("監視対象の銘柄が登録されていません。")
            return {'statusCode': 200, 'body': json.dumps('No stocks to monitor')}
            
        # ユーザーごとに通知を送るため、データを整理
        user_notifications = {}
        
        for item in items:
            user_id = item['UserID']
            symbol = item['Symbol']
            company_name = item.get('CompanyName', symbol)
            
            # 📢 変更点：リアルタイムの「今の株価」と「前日のデータ」を取得するために、
            # 期間を「1日分（間隔1分）」と「過去の歴史」の両方から安全に取得する
            ticker = yf.Ticker(symbol)
            
            # ① 今日のリアルタイムの「現在値」を取得（1分足の最新値）
            today_data = ticker.history(period="1d", interval="1m")
            if today_data.empty:
                print(f"{symbol} の本日のリアルタイムデータが取得できません。市場時間外の可能性があります。")
                continue
            current_price = today_data['Close'].iloc[-1]
            
            # ② 前営業日の「終値」を取得（安全に過去の歴史から引っ張る）
            history_data = ticker.history(period="5d")
            if len(history_data) < 2:
                print(f"{symbol} の過去データが不足しています。")
                continue
            
            # もし今日の日足データがすでにhistory_dataに入っている場合は、その1つ前が「前日終値」
            # まだ入っていない（朝イチなど）場合は、一番最後のデータが「前日終値」
            if history_data.index[-1].date() == today_data.index[-1].date():
                prev_close = history_data['Close'].iloc[-2]
            else:
                prev_close = history_data['Close'].iloc[-1]
            
            # 📢 前日比（リアルタイム現在値 vs 前日終値）の計算
            pct_change = (current_price - prev_close) / prev_close
            
            # 【重要】株価が動いている時間帯に「-2%」以下になったら即検知！
            if pct_change <= -0.02:
                change_percent = pct_change * 100
                message = f"🚨【リアルタイム急落アラート】\n{company_name} ({symbol})\n前日比: {change_percent:.2f}%\n★現在のリアルタイム株価: {current_price:.2f}円\n(前日終値: {prev_close:.2f}円)"
                
                if user_id not in user_notifications:
                    user_notifications[user_id] = []
                user_notifications[user_id].append(message)
                
        # 集計した通知をユーザーごとに送信
        for user_id, messages in user_notifications.items():
            final_message = "\n\n".join(messages)
            url = "https://api.line.me/v2/bot/message/push"
            send_line_notification(user_id, final_message, url)
            
        return {'statusCode': 200, 'body': json.dumps('Success')}
        
    except Exception as e:
        print(f"エラー発生: {e}")
        return {'statusCode': 500, 'body': json.dumps('Internal Server Error')}

def send_line_notification(user_id, text, url):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        print("LINEのアクセストークンが設定されていません。")
        return
        
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    data = {
        "to": user_id,
        "messages": [{"type": "text", "text": text}]
    }
    
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as res:
            print(f"ユーザー {user_id} へのLINE通知に成功しました。")
    except Exception as e:
        print(f"LINE通知エラー: {e}")
