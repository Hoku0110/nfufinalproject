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
import google.generativeai as genai
from PIL import Image

load_dotenv()

# 設定靜態文件目錄
front_path = os.path.join(os.path.dirname(__file__), '..', 'front')
app = Flask(__name__, static_folder=front_path, static_url_path='/front')

LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
LIFF_ID = os.getenv('LIFF_ID', '2009979323-uRaBvhWW')
LIFF_URL_BASE = os.getenv('LIFF_URL_BASE', 'https://localhost:5000')

# 臨時存儲菜單數據
menu_sessions = {}

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)
# 使用有配額的模型: gemma-3-1b (輕量) 或 gemma-3-4b (平衡)
model = genai.GenerativeModel('gemini-2.5-flash')

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

                        prompt = """
                        這是一張餐廳菜單。請幫我辨識出所有的『品項名稱』與對應的『價格』。
                        請嚴格以 JSON 陣列的格式輸出，不要加上 ```json 標籤。
                        """
                        try:
                            response = model.generate_content([prompt, img])
                            menu_json = response.text.strip()
                        except Exception as gemini_error:
                            error_msg = str(gemini_error)
                            if "429" in error_msg or "quota" in error_msg.lower():
                                reply_text = "❌ 目前Gemini API配額已用盡，請稍後再試！"
                            elif "401" in error_msg or "credential" in error_msg.lower():
                                reply_text = "❌ API金鑰設定錯誤，請檢查設定。"
                            else:
                                reply_text = f"❌ 辨識失敗：{error_msg}"
                            
                            line_bot_api.reply_message_with_http_info(
                                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
                            )
                            return

                        # 生成session ID並存儲菜單數據
                        session_id = str(uuid.uuid4())
                        try:
                            menu_data = json.loads(menu_json)
                        except:
                            menu_data = menu_json
                        menu_sessions[session_id] = menu_data

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
                                                "text": "🍔 好棒棒點餐團",
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
