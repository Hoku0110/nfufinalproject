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
# 臨時存儲訂單數據
orders_data = {}

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

# API端點：提交訂單
@app.route("/api/order", methods=['POST'])
def submit_order():
    try:
        data = request.json
        group_id = data.get('group_id')
        user_id = data.get('user_id')
        order_items = data.get('order_items', {})
        
        if not group_id or not user_id:
            return {'error': 'group_id and user_id are required'}, 400
        
        if not order_items:
            return {'error': 'order_items is empty'}, 400
        
        # 初始化群組訂單
        if group_id not in orders_data:
            orders_data[group_id] = {}
        
        # 計算使用者的訂單總額
        total = 0
        order_details = []
        for item_name, item_data in order_items.items():
            subtotal = item_data['price'] * item_data['quantity']
            total += subtotal
            order_details.append({
                'item': item_name,
                'price': item_data['price'],
                'quantity': item_data['quantity'],
                'subtotal': subtotal
            })
        
        # 存儲到群組內該使用者的訂單
        orders_data[group_id][user_id] = {
            'items': order_details,
            'total': total
        }
        
        # 計算群組總訂單統計
        group_total = 0
        group_item_count = 0
        user_count = len(orders_data[group_id])
        
        for user_order in orders_data[group_id].values():
            group_total += user_order['total']
            group_item_count += len(user_order['items'])
        
        # 格式化回應信息
        order_text = f"✅ 訂單已記錄 (群組 ID: {group_id})\n"
        order_text += f"👤 你的訂單: ${total}\n"
        order_text += f"👥 已點餐人數: {user_count}\n"
        order_text += f"📦 群組品項總數: {group_item_count}\n"
        order_text += f"💰 群組總計: ${group_total}"
        
        print(f"✅ [訂單] 群組 {group_id} - 使用者 {user_id}: ${total}")
        print(f"   群組總計: ${group_total} (已有 {user_count} 人點餐)")
        
        return {
            'status': 'success',
            'user_total': total,
            'group_total': group_total,
            'user_count': user_count,
            'order_text': order_text
        }, 200
        
    except Exception as e:
        print(f"❌ [訂單] 提交訂單錯誤: {e}")
        return {'error': str(e)}, 500

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
                        group_id = event.source.group_id  # 取得群組 ID
                        
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
                                            "uri": f"https://liff.line.me/{LIFF_ID}?session={session_id}&group={group_id}"
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
            
            elif real_command == "統整":
                group_id = event.source.group_id
                
                if not group_id:
                    reply_text = "❌ 此指令只能在群組中使用"
                elif group_id not in orders_data or not orders_data[group_id]:
                    reply_text = "📋 目前還沒有人點餐，群組訂單為空"
                else:
                    # 統整該群組的所有訂單
                    group_orders = orders_data[group_id]
                    total_amount = 0
                    order_details = []
                    
                    for user_id, user_order in group_orders.items():
                        user_total = user_order['total']
                        total_amount += user_total
                        
                        try:
                            profile = line_bot_api.get_profile(user_id)
                            user_name = profile.display_name
                        except:
                            user_name = f"使用者 {user_id[:8]}"
                        
                        items_str = "、".join([
                            f"{item['item']}({item['quantity']}份)" 
                            for item in user_order['items']
                        ])
                        
                        order_details.append({
                            'user_name': user_name,
                            'items': items_str,
                            'subtotal': user_total
                        })
                    
                    # 格式化回覆訊息
                    reply_text = "📊 群組訂單統整\n"
                    reply_text += "=" * 30 + "\n"
                    for detail in order_details:
                        reply_text += f"👤 {detail['user_name']}\n"
                        reply_text += f"   {detail['items']}\n"
                        reply_text += f"   小計: ${detail['subtotal']}\n"
                    reply_text += "=" * 30 + "\n"
                    reply_text += f"💰 群組總計: ${total_amount}"
                    
                    print(f"📊 [統整] 群組 {group_id} 訂單統計:")
                    print(reply_text)
            
            else:
                reply_text = f"我收到指令了：{real_command}\n(提示：支援指令有「開團」、「統整」)"
                
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
            )
    except Exception as e:
        print(f"LINE API 錯誤: {e}")

if __name__ == "__main__":
    app.run(port=5000)
