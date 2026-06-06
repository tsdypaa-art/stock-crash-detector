import os
import urllib.request
import json
import boto3 

# 東京リージョンのDynamoDBに接続する準備
dynamodb = boto3.resource('dynamodb', region_name='ap-northeast-1')
table = dynamodb.Table('StockTable')

def lambda_handler(event, context):
    try:
        # 1.DynamoDBからテーブルに登録されているデータを全件スキャンして取得する
        response = table.scan()
        # 2.取得したデータの中から、お目当ての「アイテムのリスト」を引っこ抜く（空なら空リスト）
        stocks = response.get('Items', [])
        
        # もしDynamoDBに1件もデータが登録されていなかったら、ここで処理を終了する
        if not stocks:
            print("DynamoDBに銘柄データが登録されていません。")
            return "データなし"

        # 3.登録されているデータの数だけ、上から1件ずつ順番にチェック（ループ処理を開始）
        for stock in stocks:
            # 設定したDynamoDBのキー名（UserIDとSymbol）から値をそれぞれ取得する
            user_id = stock.get('UserID')
            symbol = stock.get('Symbol')  # 例: "6331.T"
            
            # もしデータに銘柄コード（Symbol）が含まれていない不正なデータがあれば、スキップして次へ
            if not symbol:
                continue
            
            # Yahoo Financeから株価データを取得するためのURL（銘柄コードを自動で当てはめる）
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
                    
                    # DynamoDBに「CompanyName」があればそれを使い、無ければ銘柄コードを名前にする
                    company_name = stock.get('CompanyName', symbol)
                    
                    # クラウド（AWS）のログに、誰のどの銘柄の状況かを分かりやすく出力（デバッグ用）
                    print(f"ユーザー: {user_id}, 銘柄: {company_name}, 前日終値: {previous_close}, 現在値: {current_price}, 変動率: {price_change_rate:.2%}")
                    
                    # 暴落と判定する基準値（-2%以下になったら通知する設定）
                    CRASH_THRESHOLD = -0.02
                    
                    # 計算した変動率が、基準値（-2%）よりも低くなっているか判定
                    if price_change_rate <= CRASH_THRESHOLD:
                        # 基準を超えていたら、下の「LINE通知関数」を呼び出す（会社名、現在値、変動率を渡す）
                        send_notification(company_name, current_price, price_change_rate)
                        
            except Exception as e:
                # 特定の1銘柄でエラー（通信失敗など）が起きても、他の銘柄の監視を止めないために「continue」で次の銘柄へ飛ばす
                print(f"銘柄 {symbol} のデータ取得中にエラー: {e}")
                continue
                
        return "全銘柄のチェックが完了しました。"
            
    except Exception as e:
        # システムの根本（DynamoDB自体に繋がらないなど）でエラーが起きた場合は、ログを残して終了
        print(f"システム全体でエラーが発生しました: {e}")
        raise e

def send_notification(company_name, price, change_rate):
    # AWSの「環境変数」という隠し金庫から、LINEのアクセストークン（鍵）を取り出す
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        print("LINEのアクセストークンが設定されていないため、送信をスキップします。")
        return

    # LINEに送信するメッセージの本文を組み立てる（【変更】銘柄コードだった部分を、分かりやすい「企業名」に変更！）
    message_text = f"🚨【株価アラート】🚨\n企業: {company_name}\n現在値: ¥{price}\n前日比: {change_rate:.2%}\n基準値を超えたため通知します。"

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
            # LINE側が正常に受け取ったらログに出力
            print(f"LINE通知が正常に送信されました。")
    except Exception as e:
        # LINEへの送信中にエラーが起きた場合のログ出力
        print(f"LINE通知の送信中にエラーが発生しました: {e}")した: {e}")
