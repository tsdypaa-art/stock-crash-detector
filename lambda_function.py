import os
import urllib.request
import json
import boto3
from datetime import datetime, timedelta

def get_stock_data_native(symbol):
    """
    Yahoo FinanceのAPIから、yfinanceを使わずに直接データを取得する関数。
    ライブラリを一切使わないため、容量は数キロバイトで動作。
    """
    # 1分足(1m)で1日分(1d)のデータを取得
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            
            # APIのレスポンス構造を解析
            result = data['chart']['result'][0]
            indicators = result['indicators']['quote'][0]
            meta = result['meta']
            
            # ① 現在値（最新の1分足のClose）を取得
            closes = indicators['close']
            if not closes:
                return None, None
                
            current_price = closes[-1]
            # 最新の足がNoneの場合は、1つ前の足を見る（市場直後などの対策）
            if current_price is None and len(closes) > 1:
                current_price = closes[-2]
                
            # ② 前日終値を取得
            prev_close = meta.get('previousClose')
            
            return current_price, prev_close
            
    except Exception as e:
        print(f"{symbol} のデータ取得中にエラーが発生しました: {e}")
        return None, None

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
            
            # 📢 魔法の軽量関数で「現在値」と「前日終値」を一撃で取得
            current_price, prev_close = get_stock_data_native(symbol)
            
            if current_price is None or prev_close is None:
                print(f"{symbol} のリアルタイムデータまたは前日終値が取得できませんでした。")
                continue
            
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
