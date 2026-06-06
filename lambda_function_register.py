import os
import urllib.request
import json
import boto3

# 東京リージョンのDynamoDBに接続する準備
dynamodb = boto3.resource('dynamodb', region_name='ap-northeast-1')
table = dynamodb.Table('StockTable')

def lambda_handler(event, context):
    try:
        # 1. LINEから届いたデータ（body）を取り出す
        body = json.loads(event.get('body', '{}'))
        events = body.get('events', [])
        
        # イベントが空（LINEの接続検証など）の場合は、200番を返して終了する
        if not events:
            return {'statusCode': 200, 'body': json.dumps('No events')}
            
        for line_event in events:
            # メッセージイベント以外（友達追加など）はスキップ
            if line_event.get('type') != 'message':
                continue
                
            message = line_event.get('message', {})
            # テキストメッセージ以外（スタンプや画像）はスキップ
            if message.get('type') != 'text':
                continue
                
            # 送信した文字を取得（例：「追加 7203.T トヨタ」）
            user_text = message.get('text', '').strip()
            # 返信用のチケット（ReplyToken）を取得
            reply_token = line_event.get('replyToken')
            
            # 2. 文字列を分解する（スペースで区切る）
            # 「追加 7203.T トヨタ」 ➔ ['追加', '7203.T', 'トヨタ'] というリストになる
            words = user_text.split()
            
            # 合言葉が「追加」で、かつ単語が3つ揃っているかチェック
            if len(words) == 3 and words[0] == '追加':
                symbol = words[1]       # 2番目の文字（7203.T）
                company_name = words[2] # 3番目の文字（トヨタ）
                
                # LINEのユーザーIDを取得（これをDynamoDBのUserIDにする）
                user_id = line_event['source'].get('userId', 'unknown_user')
                
                # 3. 設計したキー構造に合わせてDynamoDBに保存！
                table.put_item(
                    Item={
                        'UserID': user_id,       # パーティションキー
                        'Symbol': symbol,        # ソートキー
                        'CompanyName': company_name # 属性（会社名）
                    }
                )
            # 成功メッセージを組み立てる
                reply_text = f"✅ 登録完了しました！\n銘柄: {symbol}\n企業名: {company_name}\n明日から自動監視を開始します。"
            
            #「消去」
            elif words[0] == '削除' and len(words) == 2:
                symbol = words[1] # 2番目の文字（7203.T）を取得
                
                # DynamoDBからあなたのUserIDとSymbolが一致するデータを削除！
                table.delete_item(
                    Key={
                        'UserID': user_id,
                        'Symbol': symbol
                    }
                )
                reply_text = f"🗑️ 削除完了しました！\n銘柄: {symbol}\nこの銘柄の自動監視を停止しました。"
                
            else:
                # 打ち方が間違っていた場合の案内メッセージ
                reply_text = "❌ 登録に失敗しました。\n\n【登録方法】\n「追加 銘柄コード 会社名」の順にスペースを開けて入力してください。\n\n（例）\n追加 7203.T トヨタ"
                
            # 4. 結果をLINEに返信する
            send_reply(reply_token, reply_text)
            
        return {'statusCode': 200, 'body': json.dumps('Success')}
        
    except Exception as e:
        print(f"エラー発生: {e}")
        return {'statusCode': 500, 'body': json.dumps('Internal Server Error')}

def send_reply(reply_token, text):
    # 環境変数からLINEのトークンを取得
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        print("LINEのアクセストークンが設定されていません。")
        return

    # LINEの「返信専用（Reply）」窓口のURL
    url = "https://api.line.me/v2/bot/message/reply"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    
    data = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}]
    }
    
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as res:
            print("LINEへの返信が成功しました。")
    except Exception as e:
        print(f"LINEへの返信中にエラー: {e}")
