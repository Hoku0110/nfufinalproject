import os
import io
import json
import uuid
import urllib.parse
from flask import Flask, request, abort, send_from_directory
from dotenv import load_dotenv
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob, 
    ReplyMessageRequest, TextMessage, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from PIL import Image
from ai_agent import extract_menu

load_dotenv()

# 設定靜態文件目錄
front_path = os.path.join(os.path.dirname(__file__), '..', 'front')
app = Flask(__name__, static_folder=front_path, static_url_path='/front')

LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LIFF_ID = os.getenv('LIFF_ID', '2009979323-uRaBvhWW')
LIFF_URL_BASE = os.getenv('LIFF_URL_BASE', 'https://localhost:5000')

# 臨時存儲菜單數據
menu_sessions = {}

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 前端路由
@app.route("/")
def serve_root():
    return send_from_directory(front_path, 'index.html')

@app.route("/liff")
@app.route("/liff/")
def serve_liff():
    return send_from_directory(front_path, 'index.html')

@app.route("/front/")
@app.route("/front/index.html")
def serve_front():
    return send_from_directory(front_path, 'index.html')

@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(front_path, filename)

# API端點：取得LIFF ID
@app.route("/api/config")
def get_config():
    return {'liffId': LIFF_ID}

# API端點：存儲菜單數據
@app.route("/api/menu", methods=['POST'])
def store_menu():
    data = request.json
    session_id = data.get('session_id')
    menu_data = data.get('menu')
    
    if session_id and menu_data:
        menu_sessions[session_id] = menu_data
        return {'status': 'success', 'session_id': session_id}
    return {'status': 'error'}, 400

# API端點：獲取菜單數據
@app.route("/api/menu/<session_id>")
def get_menu(session_id):
    if session_id in menu_sessions:
        return {'menu': menu_sessions[session_id]}
    return {'error': 'not found'}, 404

# API端點：辨識菜單（前端呼叫）
@app.route("/api/recognize", methods=['POST'])
def recognize_menu():
    session_id = request.json.get('session_id')
    
    if session_id not in menu_sessions:
        return {'error': 'session not found'}, 404
    
    session_data = menu_sessions[session_id]
    
    # 如果已經辨識完成，直接返回
    if isinstance(session_data, dict) and session_data.get('status') == 'completed':
        return {'status': 'completed', 'menu': session_data['menu_data']}
    
    # 如果還在等待辨識
    if isinstance(session_data, dict) and session_data.get('status') == 'pending':
        try:
            print(f"🤖 [API] 開始辨識 session {session_id}")
            menu_data = extract_menu(session_data['image_data'])
            
            # 存回辨識結果
            menu_sessions[session_id] = {
                'status': 'completed',
                'menu_data': menu_data
            }
            
            return {'status': 'completed', 'menu': menu_data}
        except Exception as e:
            print(f"❌ [API] 辨識失敗: {e}")
            return {'error': str(e), 'status': 'error'}, 500
    
    return {'error': 'invalid session data'}, 400

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_msg = event.message.text
    call_word = "@機器人"
    
    if not user_msg.startswith(call_word):
        return
        
    real_command = user_msg.replace(call_word, "").strip()

    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_blob_api = MessagingApiBlob(api_client)
            quoted_id = getattr(event.message, 'quoted_message_id', None)

            if real_command == "開團":
                if quoted_id:
                    try:
                        user_id = event.source.user_id
                        try:
                            profile = line_bot_api.get_profile(user_id)
                            user_name = profile.display_name
                        except Exception:
                            user_name = "群組成員"

                        message_content = line_bot_blob_api.get_message_content(quoted_id)
                        img = Image.open(io.BytesIO(message_content))

                        # 直接儲存二進制圖片，不等待辨識
                        session_id = str(uuid.uuid4())
                        print(f"💾 [APP] 存儲圖片到 session {session_id}")
                        menu_sessions[session_id] = {
                            'image_data': message_content,
                            'status': 'pending'
                        }

                        flex_dict = {
                            "type": "bubble",
                            "body": {
                                "type": "box",
                                "layout": "vertical",
                                "contents": [
                                    {
                                        "type": "box",
                                        "layout": "horizontal",
                                        "contents": [
                                            {
                                                "type": "text",
                                                "text": "🍔好棒棒點餐",
                                                "weight": "bold",
                                                "size": "lg",
                                                "flex": 1
                                            },
                                            {
                                                "type": "text",
                                                "text": f"發起人: {user_name}",
                                                "size": "xs",
                                                "color": "#aaaaaa",
                                                "align": "end",
                                                "gravity": "bottom"
                                            }
                                        ]
                                    },

                                ]
                            },
                            "footer": {
                                "type": "box",
                                "layout": "vertical",
                                "contents": [
                                    {
                                        "type": "button",
                                        "action": {
                                            "type": "uri",
                                            "label": "前往點餐",
                                            "uri": f"https://liff.line.me/{LIFF_ID}?session={session_id}"
                                        },
                                        "style": "primary",
                                        "color": "#06C755"
                                    }
                                ]
                            }
                        }

                        flex_message = FlexMessage(
                            alt_text="開團囉！請點擊卡片前往點餐",
                            contents=FlexContainer.from_dict(flex_dict)
                        )

                        line_bot_api.reply_message_with_http_info(
                            ReplyMessageRequest(
                                reply_token=event.reply_token, 
                                messages=[flex_message]
                            )
                        )
                        return

                    except Exception as e:
                        print(f"圖片處理錯誤: {e}")
                        reply_text = f"讀取或辨識圖片失敗：{str(e)}"
                else:
                    reply_text = "請先上傳一張菜單，然後「長按該菜單照片選擇回覆」，再輸入「@機器人 開團」！"
            else:
                reply_text = f"我收到指令了：{real_command}\n(提示：若要開團，請長按圖片回覆 @機器人 開團)"
                
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
            )
    except Exception as e:
        print(f"LINE API 錯誤: {e}")

if __name__ == "__main__":
    app.run(port=5000)
