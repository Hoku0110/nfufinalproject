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
    ReplyMessageRequest, PushMessageRequest, TextMessage, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from PIL import Image
from ai_agent import extract_menu, parse_voice_order
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

load_dotenv()

# 初始化 Firebase
try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(current_dir, 'finalproject-5f675-firebase-adminsdk-fbsvc-6bbed93f8d.json')
    cred = credentials.Certificate(json_path)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print(f"✅ Firebase 初始化成功")
except Exception as e:
    print(f"⚠️ Firebase 初始化失敗: {e}")

# 菜單辨識臨時存儲（用於圖片和辨識過程）
menu_sessions = {}



# 設定靜態文件目錄
front_path = os.path.join(os.path.dirname(__file__), '..', 'front')
app = Flask(__name__, static_folder=front_path, static_url_path='/front')

LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
LIFF_ID = os.getenv('LIFF_ID', '2009979323-uRaBvhWW')
LIFF_URL_BASE = os.getenv('LIFF_URL_BASE', 'https://localhost:5000')

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

# API端點：取得用戶 LINE 名稱
@app.route("/api/user-profile/<user_id>", methods=['GET'])
def get_user_profile(user_id):
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            profile = line_bot_api.get_profile(user_id)
            return {
                'displayName': profile.display_name,
                'pictureUrl': profile.picture_url,
                'statusMessage': profile.status_message,
                'userId': profile.user_id
            }, 200
    except Exception as e:
        print(f"❌ 取得用戶名稱失敗: {e}")
        return {'error': str(e)}, 400

# API端點：取得用戶餘額
@app.route("/api/user-balance/<user_id>", methods=['GET'])
def get_user_balance(user_id):
    if 'db' not in globals() or db is None:
        return {'balance': 0}, 200
    try:
        doc = db.collection('users').document(user_id).get()
        if doc.exists:
            data = doc.to_dict()
            return {'balance': data.get('balance', 0)}, 200
        return {'balance': 0}, 200
    except Exception as e:
        print(f"❌ 取得用戶餘額失敗: {e}")
        return {'balance': 0}, 500

# API端點：儲值用戶餘額
@app.route("/api/user-balance/topup", methods=['POST'])
def topup_user_balance():
    if 'db' not in globals() or db is None:
        return {'error': 'Firebase未初始化'}, 500
    try:
        data = request.json
        user_id = data.get('user_id')
        amount = data.get('amount', 0)
        
        if not user_id or amount <= 0:
            return {'error': '無效的參數'}, 400
            
        user_ref = db.collection('users').document(user_id)
        
        # 取得當前餘額並增加
        doc = user_ref.get()
        current_balance = 0
        if doc.exists:
            current_balance = doc.to_dict().get('balance', 0)
            
        new_balance = current_balance + amount
        user_ref.set({'balance': new_balance}, merge=True)
        
        return {'status': 'success', 'new_balance': new_balance}, 200
    except Exception as e:
        print(f"❌ 儲值失敗: {e}")
        return {'error': str(e)}, 500

# API端點：查詢個人可使用的店家清單
@app.route("/api/user-shops/<user_id>", methods=['GET'])
def get_user_shops(user_id):
    if 'db' not in globals() or db is None:
        print("⚠️ Firebase 未初始化，無法查詢個人店家")
        return {'status': 'success', 'shops': []}, 200
    try:
        print(f"🔍 [API] 正在查詢用戶 {user_id} 的個人店家庫...")
        shops_ref = db.collection('users').document(user_id).collection('shops').stream()
        shops_list = []
        for doc in shops_ref:
            data = doc.to_dict()
            shop_name = data.get('shop_name', doc.id)
            menu = data.get('menu', {})
            if isinstance(menu, dict):
                items = menu.get('menu_items') or menu.get('items') or menu.get('menu') or []
            elif isinstance(menu, list):
                items = menu
            else:
                items = []
            shops_list.append({
                'shop_name': shop_name,
                'item_count': len(items)
            })
        print(f"✅ [API] 用戶 {user_id} 找到 {len(shops_list)} 個店家")
        return {'status': 'success', 'shops': shops_list}, 200
    except Exception as e:
        print(f"❌ 查詢個人店家失敗: {e}")
        return {'error': str(e)}, 500

# API端點：刪除特定店家
@app.route("/api/user-shop/<user_id>/<shop_name>", methods=['DELETE'])
def delete_user_shop(user_id, shop_name):
    if 'db' not in globals() or db is None:
        return {'error': 'Firebase未初始化'}, 500
    try:
        db.collection('users').document(user_id).collection('shops').document(shop_name).delete()
        print(f"🗑️ [API] 已成功刪除用戶 {user_id} 的店家【{shop_name}】")
        return {'status': 'success', 'message': f'店家【{shop_name}】已成功刪除'}, 200
    except Exception as e:
        print(f"❌ 刪除店家失敗: {e}")
        return {'error': str(e)}, 500

# API端點：取得特定店家的菜單內容
@app.route("/api/user-shop-menu/<user_id>/<shop_name>", methods=['GET'])
def get_user_shop_menu(user_id, shop_name):
    if 'db' not in globals() or db is None:
        return {'error': 'Firebase未初始化'}, 500
    try:
        shop_doc = db.collection('users').document(user_id).collection('shops').document(shop_name).get()
        if not shop_doc.exists:
            return {'error': '店家不存在'}, 404
        shop_data = shop_doc.to_dict()
        return {'status': 'success', 'shop_name': shop_name, 'menu': shop_data.get('menu', {})}, 200
    except Exception as e:
        print(f"❌ 取得特定店家菜單失敗: {e}")
        return {'error': str(e)}, 500

# API端點：更新/儲存特定店家的菜單內容 (支援店家名稱變更)
@app.route("/api/user-shop-menu", methods=['POST'])
def save_user_shop_menu():
    if 'db' not in globals() or db is None:
        return {'error': 'Firebase未初始化'}, 500
    try:
        data = request.json
        user_id = data.get('user_id')
        old_shop_name = data.get('old_shop_name') or data.get('shop_name')
        new_shop_name = data.get('new_shop_name') or data.get('shop_name')
        menu_items = data.get('menu_items', [])
        
        if not user_id or not new_shop_name:
            return {'error': 'user_id 與 new_shop_name 為必填欄位'}, 400
        
        # 若有更改店家名稱，刪除原本名稱的舊文件
        if old_shop_name and old_shop_name != new_shop_name:
            try:
                db.collection('users').document(user_id).collection('shops').document(old_shop_name).delete()
                print(f"🔄 [API] 店家改名：已刪除舊店家檔【{old_shop_name}】")
            except Exception as e:
                print(f"⚠️ 刪除舊店家檔失敗: {e}")

        menu_data = {
            'shop_name': new_shop_name,
            'menu_items': menu_items
        }
        
        db.collection('users').document(user_id).collection('shops').document(new_shop_name).set({
            'shop_name': new_shop_name,
            'menu': menu_data,
            'updated_at': firestore.SERVER_TIMESTAMP
        }, merge=True)
        
        print(f"✅ [API] 已成功儲存用戶 {user_id} 的店家【{new_shop_name}】菜單 (共 {len(menu_items)} 項)")
        return {'status': 'success', 'shop_name': new_shop_name, 'item_count': len(menu_items)}, 200
    except Exception as e:
        print(f"❌ 儲存店家菜單失敗: {e}")
        return {'error': str(e)}, 500

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

# API端點：獲取菜單數據 (支援從 Firebase 讀取永久保存的 Session)
@app.route("/api/menu/<session_id>")
def get_menu(session_id):
    session_data = menu_sessions.get(session_id)
    if not session_data and 'db' in globals() and db:
        try:
            doc = db.collection('sessions').document(session_id).get()
            if doc.exists:
                session_data = doc.to_dict()
                menu_sessions[session_id] = session_data  # 快取至記憶體
        except Exception as e:
            print(f"⚠️ 讀取 Firebase session 失敗: {e}")
            
    if session_data:
        is_closed = session_data.get('is_closed', False) if isinstance(session_data, dict) else False
        initiator_id = session_data.get('initiator_id', '') if isinstance(session_data, dict) else ''
        menu_content = session_data.get('menu_data') if isinstance(session_data, dict) and 'menu_data' in session_data else session_data
        return {'menu': menu_content, 'is_closed': is_closed, 'initiator_id': initiator_id}

    return {'error': 'not found'}, 404

# API端點：辨識菜單（前端呼叫，支援雙重快取與 Firebase 永久儲存）
@app.route("/api/recognize", methods=['POST'])
def recognize_menu():
    session_id = request.json.get('session_id')
    
    session_data = menu_sessions.get(session_id)
    if not session_data and 'db' in globals() and db:
        try:
            doc = db.collection('sessions').document(session_id).get()
            if doc.exists:
                session_data = doc.to_dict()
                menu_sessions[session_id] = session_data
        except Exception as e:
            print(f"⚠️ 從 Firebase 讀取 session 失敗: {e}")

    if not session_data:
        return {'error': 'session not found'}, 404
    
    is_closed = session_data.get('is_closed', False) if isinstance(session_data, dict) else False
    initiator_id = session_data.get('initiator_id', '') if isinstance(session_data, dict) else ''

    # 如果已經辨識完成，直接返回
    if isinstance(session_data, dict) and session_data.get('status') == 'completed':
        return {
            'status': 'completed',
            'menu': session_data.get('menu_data'),
            'is_closed': is_closed,
            'initiator_id': initiator_id
        }
    
    # 如果還在等待辨識
    if isinstance(session_data, dict) and session_data.get('status') == 'pending':
        try:
            print(f"🤖 [API] 開始辨識 session {session_id}")
            menu_data = extract_menu(session_data['image_data'])
            
            # 存回辨識結果至記憶體與 Firebase
            session_result = {
                'status': 'completed',
                'menu_data': menu_data,
                'is_closed': is_closed,
                'initiator_id': initiator_id
            }
            menu_sessions[session_id] = session_result
            
            if 'db' in globals() and db:
                try:
                    db.collection('sessions').document(session_id).set(session_result, merge=True)
                    print(f"☁️ [Session] 已更新辨識結果至 Firebase session {session_id}")
                except Exception as se:
                    print(f"⚠️ 更新 Firebase session 失敗: {se}")

            return {
                'status': 'completed',
                'menu': menu_data,
                'is_closed': is_closed,
                'initiator_id': initiator_id
            }
        except Exception as e:
            print(f"❌ [API] 辨識失敗: {e}")
            return {'error': str(e), 'status': 'error'}, 500
    
    return {'error': 'invalid session data'}, 400

# API端點：宣告結單 (前端手動發起結單)
@app.route("/api/close-session", methods=['POST'])
def close_session():
    try:
        data = request.json
        session_id = data.get('session_id')
        group_id = data.get('group_id')
        user_id = data.get('user_id')
        
        if not session_id and not group_id:
            return {'error': 'session_id or group_id is required'}, 400

        if session_id and session_id in menu_sessions:
            if isinstance(menu_sessions[session_id], dict):
                menu_sessions[session_id]['is_closed'] = True
        
        if 'db' in globals() and db:
            if session_id:
                db.collection('sessions').document(session_id).set({'is_closed': True}, merge=True)
            if group_id:
                db.collection('groups').document(group_id).set({'is_closed': True}, merge=True)
                if user_id:
                    db.collection('groups').document(group_id).collection('initiators').document(user_id).set({'is_closed': True}, merge=True)
        
        print(f"🔒 [結單 API] Session {session_id} (群組 {group_id}) 已宣告結單！")
        return {'status': 'success', 'message': '已成功結單'}, 200
    except Exception as e:
        print(f"❌ 結單失敗: {e}")
        return {'error': str(e)}, 500

# API端點：查詢 Session 狀態 (輕量化 Ajax 輪詢)
@app.route("/api/session-status/<session_id>")
def get_session_status(session_id):
    session_data = menu_sessions.get(session_id)
    if not session_data and 'db' in globals() and db:
        try:
            doc = db.collection('sessions').document(session_id).get()
            if doc.exists:
                session_data = doc.to_dict()
                menu_sessions[session_id] = session_data
        except Exception as e:
            print(f"⚠️ 輪詢 Session 失敗: {e}")
            
    if session_data and isinstance(session_data, dict):
        return {'is_closed': session_data.get('is_closed', False)}, 200
        
    return {'is_closed': False}, 200

# API端點：查詢個人歷史訂單紀錄
@app.route("/api/order-history/<user_id>", methods=['GET'])
def get_order_history(user_id):
    try:
        if 'db' not in globals() or not db:
            return {'history': []}, 200
        docs = (
            db.collection('users').document(user_id)
            .collection('order_history')
            .order_by('completed_at', direction=firestore.Query.DESCENDING)
            .limit(30)
            .stream()
        )
        history = []
        for doc in docs:
            d = doc.to_dict()
            d['doc_id'] = doc.id  # 回傳文件 ID 供前端刪除用
            ts = d.get('completed_at')
            if ts and hasattr(ts, 'isoformat'):
                d['completed_at'] = ts.isoformat()
            history.append(d)
        return {'history': history}, 200
    except Exception as e:
        print(f"❌ 查詢歷史訂單失敗: {e}")
        return {'error': str(e)}, 500

# API端點：刪除單筆歷史訂單
@app.route("/api/order-history/<user_id>/<doc_id>", methods=['DELETE'])
def delete_order_history(user_id, doc_id):
    try:
        if 'db' not in globals() or not db:
            return {'error': 'DB not available'}, 500
        db.collection('users').document(user_id).collection('order_history').document(doc_id).delete()
        print(f"🗑️ 已刪除使用者 {user_id} 的歷史訂單 {doc_id}")
        return {'status': 'deleted'}, 200
    except Exception as e:
        print(f"❌ 刪除歷史訂單失敗: {e}")
        return {'error': str(e)}, 500

# API端點：確認某 session 是否已完成歸檔
@app.route("/api/session-completed/<user_id>/<session_id>", methods=['GET'])
def check_session_completed(user_id, session_id):
    try:
        if 'db' not in globals() or not db:
            return {'completed': False}, 200
        docs = list(
            db.collection('users').document(user_id)
            .collection('order_history')
            .where(filter=FieldFilter('session_id', '==', session_id))
            .limit(1)
            .stream()
        )
        return {'completed': len(docs) > 0}, 200
    except Exception as e:
        print(f"❌ 確認完成狀態失敗: {e}")
        return {'completed': False}, 500

# API端點：查詢用戶已有訂單 (支援 session 專屬查詢)
@app.route("/api/order/<group_id>/<user_id>", methods=['GET'])
def get_user_order(group_id, user_id):
    try:
        session_id = request.args.get('session_id')
        
        # 1. 若有 session_id，只查當前 session，絕不 fallback 到舊團購
        if session_id and 'db' in globals() and db:
            order_doc = db.collection('sessions').document(session_id).collection('orders').document(user_id).get()
            if order_doc.exists:
                return {'status': 'found', 'order': order_doc.to_dict()}, 200
            # 有 session_id 但查無訂單 → 新的一次，直接回傳 not_found
            return {'status': 'not_found'}, 404

        # 2. 無 session_id 才從群組層級查詢（舊版相容）
        if 'db' in globals() and db:
            order_doc = db.collection('groups').document(group_id).collection('orders').document(user_id).get()
            if order_doc.exists:
                return {'status': 'found', 'order': order_doc.to_dict()}, 200
        
        return {'status': 'not_found'}, 404
    except Exception as e:
        print(f"❌ 查詢訂單錯誤: {e}")
        return {'error': str(e)}, 500

# API端點：提交訂單（允許修改，發起人獨立隔離）
@app.route("/api/order", methods=['POST'])
def submit_order():
    try:
        data = request.json
        session_id = data.get('session_id')
        group_id = data.get('group_id')
        user_id = data.get('user_id')
        user_name = data.get('user_name', 'Unknown')
        shop_name = data.get('shop_name', '')
        order_items = data.get('order_items', {})
        
        if not group_id or not user_id:
            return {'error': 'group_id and user_id are required'}, 400

        # 先查詢 session 資料，取得 initiator_id 並檢查結單狀態
        initiator_id = None
        if session_id:
            session_data = menu_sessions.get(session_id)
            if not session_data and 'db' in globals() and db:
                try:
                    s_doc = db.collection('sessions').document(session_id).get()
                    if s_doc.exists:
                        session_data = s_doc.to_dict()
                except Exception as se:
                    print(f"⚠️ 讀取 session 失敗: {se}")

            if session_data:
                initiator_id = session_data.get('initiator_id')
                if session_data.get('is_closed'):
                    return {'error': '已結單無法點餐'}, 400

        # 檢查群組級別結單狀態
        if group_id and 'db' in globals() and db:
            try:
                g_doc = db.collection('groups').document(group_id).get()
                if g_doc.exists and g_doc.to_dict().get('is_closed'):
                    return {'error': '已結單無法點餐'}, 400
            except Exception as ge:
                print(f"⚠️ 檢查群組狀態失敗: {ge}")

        # --- 處理餘額扣除 (先取得新舊訂單差額) ---
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
            
        previous_total = 0
        current_balance = 0
        new_balance = 0
        if 'db' in globals() and db:
            user_ref = db.collection('users').document(user_id)
            user_doc = user_ref.get()
            if user_doc.exists:
                current_balance = user_doc.to_dict().get('balance', 0)
                
            if session_id:
                try:
                    old_doc = db.collection('sessions').document(session_id).collection('orders').document(user_id).get()
                    if old_doc.exists:
                        previous_total = old_doc.to_dict().get('total', 0)
                except Exception as e:
                    print(f"⚠️ 讀取舊訂單失敗: {e}")

            diff = total - previous_total
            
            # 檢查餘額
            if order_items and diff > 0 and current_balance < diff:
                return {'error': '餘額不足，請先儲值'}, 400
                
            new_balance = current_balance - diff
            try:
                user_ref.set({'balance': new_balance}, merge=True)
                print(f"💰 [餘額更新] 用戶 {user_name} 餘額從 {current_balance} 變更為 {new_balance}")
            except Exception as e:
                print(f"⚠️ 更新餘額失敗: {e}")
                return {'error': '更新餘額失敗'}, 500

        if not order_items:
            # 購物車為空 → 刪除該使用者的既有訂單（三個集合都刪）
            if session_id and 'db' in globals() and db:
                try:
                    db.collection('sessions').document(session_id).collection('orders').document(user_id).delete()
                    print(f"🗑️ [清除訂單] session {session_id} 的成員 {user_id} 訂單已刪除")
                except Exception as de:
                    print(f"⚠️ 刪除 session 訂單失敗: {de}")
            if initiator_id and 'db' in globals() and db:
                try:
                    db.collection('groups').document(group_id).collection('initiators').document(initiator_id).collection('orders').document(user_id).delete()
                except Exception: pass
            if 'db' in globals() and db:
                try:
                    db.collection('groups').document(group_id).collection('orders').document(user_id).delete()
                except Exception: pass
            return {'status': 'cleared', 'message': '訂單已清除', 'new_balance': new_balance}, 200



        order_payload = {
            'user_name': user_name,
            'shop_name': shop_name,
            'items': order_details,
            'total': total,
            'session_id': session_id,
            'timestamp': firestore.SERVER_TIMESTAMP
        }

        # 1. 若有特定 initiator_id，寫入該發起人的專屬點餐資料匣
        if initiator_id and 'db' in globals() and db:
            try:
                db.collection('groups').document(group_id).collection('initiators').document(initiator_id).collection('orders').document(user_id).set(order_payload)
                print(f"☁️ [訂單] 已將成員 {user_name} 的訂單寫入發起人 {initiator_id} 的專屬資料匣")
            except Exception as ie:
                print(f"⚠️ 寫入發起人 orders 失敗: {ie}")

        # 2. 若有 session_id，寫入 sessions/{session_id}/orders/{user_id}
        if session_id and 'db' in globals() and db:
            try:
                db.collection('sessions').document(session_id).collection('orders').document(user_id).set(order_payload)
            except Exception as se:
                print(f"⚠️ 寫入 session orders 失敗: {se}")
        
        # 3. 備份寫入 groups/{group_id}/orders/{user_id}
        if 'db' in globals() and db:
            db.collection('groups').document(group_id).collection('orders').document(user_id).set(order_payload)

        # 計算統計人數與金額
        user_count = 1
        group_total = total
        if session_id and 'db' in globals() and db:
            try:
                orders_snapshot = db.collection('sessions').document(session_id).collection('orders').stream()
                orders_list = [o.to_dict() for o in orders_snapshot]
                if orders_list:
                    group_total = sum(o['total'] for o in orders_list)
                    user_count = len(orders_list)
            except Exception:
                pass

        print(f"✅ [訂單] 群組 {group_id} - 成員 {user_name}: ${total}")
        
        return {
            'status': 'success',
            'user_total': total,
            'group_total': group_total,
            'user_count': user_count,
            'new_balance': new_balance
        }, 200
    except Exception as e:
        print(f"❌ 提交訂單失敗: {e}")
        return {'error': str(e)}, 500

# API端點：聊天機器人 - 輸入品項名稱（可多個），回傳有此品項的店家
@app.route("/api/chat", methods=['POST'])
def chat_recommend():
    try:
        data = request.json
        user_id = data.get('user_id')
        raw_input = data.get('message', '').strip()

        if not user_id:
            return {'error': '請先登入'}, 400
        if not raw_input:
            return {'error': '請輸入品項名稱'}, 400

        # 支援空白、全形空白、逗號、頓號分隔多個品項
        import re
        keywords = [k.strip() for k in re.split(r'[,，、\s　]+', raw_input) if k.strip()]

        # 從 Firebase 讀取該用戶所有店家的菜單
        shops_data = []
        if 'db' in globals() and db:
            try:
                shops_ref = db.collection('users').document(user_id).collection('shops').stream()
                for doc in shops_ref:
                    shop = doc.to_dict()
                    shop_name = shop.get('shop_name', doc.id)
                    menu = shop.get('menu', {})
                    if isinstance(menu, dict):
                        items = menu.get('menu_items') or menu.get('items') or menu.get('menu') or []
                    elif isinstance(menu, list):
                        items = menu
                    else:
                        items = []
                    if items:
                        shops_data.append({
                            'shop_name': shop_name,
                            'items': items
                        })
            except Exception as e:
                print(f"⚠️ [Chat] 讀取用戶店家失敗: {e}")

        if not shops_data:
            return {
                'reply': '您的個人店家庫目前是空的！\n\n請先在 LINE 上傳菜單建立店家：\n「@機器人 上傳 店家名稱」',
                'found': False
            }, 200

        # AND 邏輯：只列出同時擁有所有搜尋品項的店家
        matched_shops = []
        for shop in shops_data:
            # 取出該店所有品項名稱
            shop_item_names = set()
            for item in shop['items']:
                item_name = item.get('item_name') or item.get('name') or item.get('item') or ''
                if item_name:
                    shop_item_names.add(item_name)

            # 確認每個關鍵字都在該店的品項裡
            if all(kw in shop_item_names for kw in keywords):
                matched_shops.append(shop['shop_name'])

        print(f"🔍 [Chat] 用戶 {user_id} 查詢 {keywords}（AND），找到 {len(matched_shops)} 間店")

        kw_str = '、'.join(f'「{k}」' for k in keywords)
        if matched_shops:
            shop_list = '\n'.join(f'🏪 {s}' for s in matched_shops)
            reply = f'同時有賣 {kw_str} 的店家：\n\n{shop_list}'
        else:
            reply = f'您的店家庫中沒有同時賣 {kw_str} 的店家。\n\n（提示：請輸入完整品項名稱）'

        return {'reply': reply, 'found': bool(matched_shops)}, 200

    except Exception as e:
        print(f"❌ [Chat API] 錯誤: {e}")
        return {'error': str(e)}, 500


# API端點：語音點餐解析（前端語音辨識後送來文字，後端比對菜單回傳品項與數量）
@app.route("/api/voice-order", methods=['POST'])
def voice_order():
    try:
        data = request.json
        voice_text = (data.get('voice_text') or '').strip()
        menu_items = data.get('menu_items', [])
        session_id = data.get('session_id')
        current_cart = data.get('current_cart', {})  # 目前購物車內容

        if not voice_text:
            return {'error': '語音文字為空，請重新說一次'}, 400

        if not menu_items:
            return {'error': '目前沒有可用的菜單，請先載入菜單'}, 400

        # 檢查是否已結單
        if session_id:
            session_data = menu_sessions.get(session_id)
            if not session_data and 'db' in globals() and db:
                try:
                    doc = db.collection('sessions').document(session_id).get()
                    if doc.exists:
                        session_data = doc.to_dict()
                except Exception as se:
                    print(f"⚠️ [語音點餐] 讀取 session 失敗: {se}")
            if session_data and isinstance(session_data, dict) and session_data.get('is_closed'):
                return {'error': '已結單，無法繼續點餐'}, 400

        print(f"🎙️ [語音點餐 API] 文字：「{voice_text}」，菜單：{len(menu_items)} 項，購物車：{len(current_cart)} 項")
        result = parse_voice_order(voice_text, menu_items, current_cart)

        if result.get('error'):
            # 如果錯誤訊息包含 503，提示系統忙碌
            err_msg = result['error']
            if '503' in err_msg or 'UNAVAILABLE' in err_msg:
                err_msg = '目前 AI 系統忙碌中，請稍後再試'
            return {'error': err_msg}, 500

        return {
            'status': 'success',
            'voice_text': voice_text,
            'add': result.get('add', []),
            'remove': result.get('remove', []),
            'unrecognized': result.get('unrecognized', []),
        }, 200

    except Exception as e:
        print(f"❌ [語音點餐 API] 錯誤: {e}")
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

            if real_command.startswith("上傳"):
                shop_name = real_command.replace("上傳", "", 1).strip()
                if not shop_name:
                    reply_text = "❌ 請輸入店家名稱！\n格式：「@機器人 上傳 (店家名稱)」"
                elif not quoted_id:
                    reply_text = f"請先上傳一張菜單照片，然後「長按該菜單照片選擇回覆」，再輸入「@機器人 上傳 {shop_name}」！"
                else:
                    try:
                        user_id = event.source.user_id
                        try:
                            profile = line_bot_api.get_profile(user_id)
                            user_name = profile.display_name
                        except Exception:
                            user_name = "使用者"

                        message_content = line_bot_blob_api.get_message_content(quoted_id)
                        
                        # 先發送即時提示訊息告知已收到上傳
                        to_target = getattr(event.source, 'group_id', None) or user_id
                        try:
                            line_bot_api.push_message(
                                PushMessageRequest(
                                    to=to_target,
                                    messages=[TextMessage(text=f"⏳ 收到【{shop_name}】菜單照片！\n🤖 Gemini AI 正在辨識中，請稍候...")]
                                )
                            )
                        except Exception as pe:
                            print(f"⚠️ 發送即時提示訊息失敗: {pe}")

                        print(f"🤖 [上傳] 正在透過 Gemini AI 辨識【{shop_name}】菜單...")
                        
                        # 呼叫 Gemini 辨識菜單
                        raw_menu_data = extract_menu(message_content)
                        
                        # 正確相容與提取餐點品項列表
                        if isinstance(raw_menu_data, dict):
                            raw_menu_data['shop_name'] = shop_name
                            items_list = raw_menu_data.get('menu_items') or raw_menu_data.get('items') or raw_menu_data.get('menu') or []
                            menu_data = raw_menu_data
                        elif isinstance(raw_menu_data, list):
                            items_list = raw_menu_data
                            menu_data = {'shop_name': shop_name, 'menu_items': items_list}
                        else:
                            items_list = []
                            menu_data = {'shop_name': shop_name, 'menu_items': []}

                        item_count = len(items_list)

                        # 存入 Firebase 個人使用者資料庫: users/{user_id}/shops/{shop_name}
                        user_ref = db.collection('users').document(user_id)
                        user_ref.set({
                            'user_name': user_name,
                            'updated_at': firestore.SERVER_TIMESTAMP
                        }, merge=True)
                        
                        user_ref.collection('shops').document(shop_name).set({
                            'shop_name': shop_name,
                            'menu': menu_data,
                            'created_at': firestore.SERVER_TIMESTAMP
                        })

                        if item_count > 0:
                            reply_text = f"✅ 成功為 {user_name} 建立專屬店家【{shop_name}】！\n已由 Gemini 辨識並儲存 {item_count} 項餐點資料。\n\n💡 隨時輸入「@機器人 團購 {shop_name}」即可發起團購點餐！"
                        else:
                            reply_text = f"⚠️ 已為 {user_name} 建立店家【{shop_name}】，但圖片未能成功辨識出餐點項目 (0 項)。\n請確認圖片清晰度後重新長按圖片並輸入：「@機器人 上傳 {shop_name}」進行覆蓋。"

                        print(f"✅ [上傳成功] 用戶 {user_id} ({user_name}) 上傳了店家 {shop_name} (共 {item_count} 項)")
                    except Exception as e:
                        print(f"❌ 上傳菜單處理失敗: {e}")
                        reply_text = f"❌ 上傳【{shop_name}】菜單失敗：{str(e)}"

            elif real_command.startswith("團購"):
                shop_name = real_command.replace("團購", "", 1).strip()
                if not shop_name:
                    reply_text = "❌ 請輸入店家名稱！\n格式：「@機器人 團購 (店家名稱)」"
                else:
                    user_id = event.source.user_id
                    group_id = getattr(event.source, 'group_id', None) or user_id
                    
                    try:
                        profile = line_bot_api.get_profile(user_id)
                        user_name = profile.display_name
                    except Exception:
                        user_name = "使用者"

                    # 從 Firebase 查詢發起人自己的個人資料庫 users/{user_id}/shops/{shop_name}
                    shop_doc = db.collection('users').document(user_id).collection('shops').document(shop_name).get()
                    
                    if not shop_doc.exists:
                        reply_text = (
                            f"⛔ 無法發起團購！\n"
                            f"您的個人資料庫中尚未建立【{shop_name}】的菜單。\n\n"
                            f"💡 提示：請先長按菜單照片選擇回覆，並輸入：\n"
                            f"「@機器人 上傳 {shop_name}」即可建立您專屬的店家菜單！"
                        )
                    else:
                        shop_data = shop_doc.to_dict()
                        menu_data = shop_data.get('menu', [])
                        
                        # 建立 Session (狀態設為 completed，雙重寫入記憶體與 Firebase 雲端資料庫)
                        session_id = str(uuid.uuid4())
                        print(f"💾 [團購] 發起店家 {shop_name} 團購 session {session_id}")
                        session_payload = {
                            'status': 'completed',
                            'is_closed': False,
                            'menu_data': menu_data,
                            'shop_name': shop_name,
                            'initiator_id': user_id,
                            'initiator_name': user_name
                        }
                        menu_sessions[session_id] = session_payload
                        
                        if 'db' in globals() and db:
                            try:
                                db.collection('sessions').document(session_id).set(session_payload)
                                print(f"☁️ [Session] 已將團購 session {session_id} 寫入 Firebase")
                            except Exception as se:
                                print(f"⚠️ 寫入 Firebase session 失敗: {se}")

                        # 清除該發起人原本的舊訂單明細與紀錄當前團購發起資訊
                        if getattr(event.source, 'group_id', None):
                            try:
                                # 1. 紀錄發起人專屬文件 (initiators/user_id)
                                init_doc_ref = db.collection('groups').document(group_id).collection('initiators').document(user_id)
                                init_doc_ref.set({
                                    'initiator_id': user_id,
                                    'initiator_name': user_name,
                                    'shop_name': shop_name,
                                    'session_id': session_id,
                                    'updated_at': firestore.SERVER_TIMESTAMP
                                })

                                # 2. 更新群組最新活動標示 (groups/group_id)，並重置結單狀態
                                db.collection('groups').document(group_id).set({
                                    'initiator_id': user_id,
                                    'initiator_name': user_name,
                                    'shop_name': shop_name,
                                    'session_id': session_id,
                                    'is_closed': False,
                                    'has_summarized': False,
                                    'updated_at': firestore.SERVER_TIMESTAMP
                                }, merge=True)

                                # 3. 清除該發起人舊有明細
                                orders_ref = init_doc_ref.collection('orders')
                                docs = orders_ref.stream()
                                for doc in docs:
                                    doc.reference.delete()
                                print(f"🗑️ [團購] 發起人 {user_name} 已發起新團購【{shop_name}】，發起人舊明細已清空！")
                            except Exception as e:
                                print(f"⚠️ 清除與記錄發起人失敗: {e}")

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
                                                "text": f"🍔【{shop_name}】團購點餐",
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
                                    }
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
                            alt_text=f"【{shop_name}】團購開跑囉！請點擊卡片前往點餐",
                            contents=FlexContainer.from_dict(flex_dict)
                        )

                        line_bot_api.reply_message_with_http_info(
                            ReplyMessageRequest(
                                reply_token=event.reply_token, 
                                messages=[flex_message]
                            )
                        )
                        return

            elif real_command == "開團":
                if quoted_id:
                    try:
                        user_id = event.source.user_id
                        group_id = event.source.group_id  # 取得群組 ID
                        
                        try:
                            profile = line_bot_api.get_profile(user_id)
                            user_name = profile.display_name
                        except Exception:
                            user_name = "發起人"

                        message_content = line_bot_blob_api.get_message_content(quoted_id)
                        img = Image.open(io.BytesIO(message_content))

                        # 直接儲存二進制圖片，不等待辨識
                        session_id = str(uuid.uuid4())
                        print(f"💾 [APP] 存儲圖片到 session {session_id}")
                        menu_sessions[session_id] = {
                            'image_data': message_content,
                            'status': 'pending',
                            'is_closed': False,
                            'initiator_id': user_id,
                            'initiator_name': user_name
                        }

                        # 清除該發起人原本的舊訂單明細與紀錄當前團購發起資訊
                        try:
                            # 1. 紀錄發起人專屬文件 (initiators/user_id)
                            init_doc_ref = db.collection('groups').document(group_id).collection('initiators').document(user_id)
                            init_doc_ref.set({
                                'initiator_id': user_id,
                                'initiator_name': user_name,
                                'session_id': session_id,
                                'updated_at': firestore.SERVER_TIMESTAMP
                            })

                            # 2. 更新群組最新活動標示 (groups/group_id)，並重置結單狀態
                            db.collection('groups').document(group_id).set({
                                'initiator_id': user_id,
                                'initiator_name': user_name,
                                'session_id': session_id,
                                'is_closed': False,
                                'has_summarized': False,
                                'updated_at': firestore.SERVER_TIMESTAMP
                            }, merge=True)

                            # 同步將新 session 寫入 Firestore，確保 is_closed 從 False 開始
                            db.collection('sessions').document(session_id).set({
                                'initiator_id': user_id,
                                'initiator_name': user_name,
                                'group_id': group_id,
                                'is_closed': False,
                                'created_at': firestore.SERVER_TIMESTAMP
                            })

                            # 3. 清除該發起人舊有明細
                            orders_ref = init_doc_ref.collection('orders')
                            docs = orders_ref.stream()
                            for doc in docs:
                                doc.reference.delete()
                            print(f"🗑️ [開團] 發起人 {user_name} 已發起新團購，發起人舊明細已清空！")
                        except Exception as e:
                            print(f"⚠️ 清除與記錄發起人失敗: {e}")

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
                caller_user_id = event.source.user_id
                
                if not group_id:
                    reply_text = "❌ 此指令只能在群組中使用"
                else:
                    target_initiator_id = None
                    target_initiator_name = "發起人"
                    target_shop_name = ""
                    target_session_id = None

                    # 1. 檢查發送者 (caller) 本身是否有發起中的專屬團購
                    caller_init_doc = db.collection('groups').document(group_id).collection('initiators').document(caller_user_id).get()
                    if caller_init_doc.exists:
                        init_info = caller_init_doc.to_dict()
                        target_initiator_id = caller_user_id
                        target_initiator_name = init_info.get('initiator_name', '發起人')
                        target_shop_name = init_info.get('shop_name', '')
                        target_session_id = init_info.get('session_id')
                    else:
                        # 2. 若發送者非發起人，嘗試取得群組內最新一筆活動團購資訊
                        group_doc = db.collection('groups').document(group_id).get()
                        if group_doc.exists:
                            group_info = group_doc.to_dict()
                            target_initiator_id = group_info.get('initiator_id')
                            target_initiator_name = group_info.get('initiator_name', '發起人')
                            target_shop_name = group_info.get('shop_name', '')
                            target_session_id = group_info.get('session_id')

                    group_orders = {}

                    # 3. 讀取發起人的點餐紀錄 (優先從 session/initiator 獨立資料庫)
                    if target_session_id and 'db' in globals() and db:
                        try:
                            s_orders = db.collection('sessions').document(target_session_id).collection('orders').stream()
                            group_orders = {order.id: order.to_dict() for order in s_orders}
                        except Exception as se:
                            print(f"⚠️ 讀取 session 專屬訂單失敗: {se}")

                    if not group_orders and target_initiator_id and 'db' in globals() and db:
                        try:
                            i_orders = db.collection('groups').document(group_id).collection('initiators').document(target_initiator_id).collection('orders').stream()
                            group_orders = {order.id: order.to_dict() for order in i_orders}
                        except Exception as ie:
                            print(f"⚠️ 讀取 initiator 專屬訂單失敗: {ie}")

                    if not group_orders:
                        if target_initiator_id:
                            reply_text = f"📋 目前【{target_initiator_name}】發起的團購尚未有人點餐，訂單明細為空"
                        else:
                            reply_text = "📋 目前尚無任何進行中的團購。請先輸入「@機器人 團購 (店家名稱)」發起團購！"
                    else:
                        # 統整該發起人團購的所有訂單
                        total_amount = 0
                        order_details = []
                        global_item_summary = {}  # 全群組餐點品項總計 (品項 -> 數量)

                        for user_id, user_order in group_orders.items():
                            user_total = user_order['total']
                            total_amount += user_total
                            shop_name = user_order.get('shop_name', '')
                            if shop_name and not target_shop_name:
                                target_shop_name = shop_name
                            
                            # 優先使用 Firestore 保存的用戶名，其次嘗試 LINE Bot API
                            user_name = user_order.get('user_name', '')
                            if not user_name or user_name == 'Unknown':
                                try:
                                    profile = line_bot_api.get_profile(user_id)
                                    user_name = profile.display_name
                                except:
                                    user_name = f"使用者 {user_id[-4:]}"  # 只顯示後 4 位
                            
                            # 統計個人與全群組品項
                            user_items = user_order.get('items', [])
                            items_str_list = []
                            for item in user_items:
                                item_name = item['item']
                                qty = item['quantity']
                                items_str_list.append(f"{item_name}({qty}份)")
                                
                                # 累加到全群組統計
                                global_item_summary[item_name] = global_item_summary.get(item_name, 0) + qty

                            order_details.append({
                                'user_name': user_name,
                                'items': "、".join(items_str_list),
                                'subtotal': user_total
                            })
                        
                        # 格式化回覆訊息 (明確標示該次團購的發起人)
                        reply_text = "📊 團購訂單統整\n"
                        reply_text += f"👑 發起人：{target_initiator_name}\n"
                        if target_shop_name:
                            reply_text += f"🏪 店家：{target_shop_name}\n"
                        reply_text += "=" * 28 + "\n"
                        
                        # 1. 個人明細
                        reply_text += "👤 【個人點餐明細】\n"
                        for detail in order_details:
                            reply_text += f"• {detail['user_name']}：{detail['items']} (${detail['subtotal']})\n"
                        
                        reply_text += "-" * 28 + "\n"
                        
                        # 2. 全群組品項加總
                        total_items_count = sum(global_item_summary.values())
                        reply_text += "🍱 【全群組餐點總計】\n"
                        for item_name, qty in global_item_summary.items():
                            reply_text += f"• {item_name} × {qty} 份\n"
                        
                        reply_text += "=" * 28 + "\n"
                        reply_text += f"🔢 總餐點數：{total_items_count} 份\n"
                        reply_text += f"💰 總金額：${total_amount}"

                        # 統整成功！記錄已統整狀態
                        try:
                            db.collection('groups').document(group_id).set({'has_summarized': True}, merge=True)
                            print(f"✅ [統整] 群組 {group_id} 已記錄統整完成")
                        except Exception as fse:
                            print(f"⚠️ 記錄統整狀態失敗: {fse}")

                        print(f"📊 [統整] 群組 {group_id} (發起人: {target_initiator_name}) 訂單統計:")
                        print(reply_text)
            
            elif real_command == "結單":
                group_id = event.source.group_id
                caller_user_id = event.source.user_id
                
                if not group_id:
                    reply_text = "❌ 此指令只能在群組中使用"
                else:
                    target_initiator_id = None
                    target_initiator_name = "發起人"
                    target_shop_name = ""
                    target_session_id = None

                    # 1. 檢查發送者 (caller) 本身是否有發起中的專屬團購
                    caller_init_doc = db.collection('groups').document(group_id).collection('initiators').document(caller_user_id).get()
                    if caller_init_doc.exists:
                        init_info = caller_init_doc.to_dict()
                        target_initiator_id = caller_user_id
                        target_initiator_name = init_info.get('initiator_name', '發起人')
                        target_shop_name = init_info.get('shop_name', '')
                        target_session_id = init_info.get('session_id')
                    else:
                        # 2. 若發送者非發起人，嘗試取得群組內最新一筆活動團購資訊
                        group_doc = db.collection('groups').document(group_id).get()
                        if group_doc.exists:
                            group_info = group_doc.to_dict()
                            target_initiator_id = group_info.get('initiator_id')
                            target_initiator_name = group_info.get('initiator_name', '發起人')
                            target_shop_name = group_info.get('shop_name', '')
                            target_session_id = group_info.get('session_id')

                    # 將 Session 及群組狀態更新為結單 (is_closed = True)
                    if target_session_id:
                        if target_session_id in menu_sessions and isinstance(menu_sessions[target_session_id], dict):
                            menu_sessions[target_session_id]['is_closed'] = True
                        if 'db' in globals() and db:
                            try:
                                db.collection('sessions').document(target_session_id).set({'is_closed': True}, merge=True)
                            except Exception as se:
                                print(f"⚠️ 更新 session 結單狀態失敗: {se}")

                        if 'db' in globals() and db:
                            try:
                                db.collection('groups').document(group_id).set({'is_closed': True}, merge=True)
                                if target_initiator_id:
                                    db.collection('groups').document(group_id).collection('initiators').document(target_initiator_id).set({'is_closed': True}, merge=True)
                            except Exception as ge:
                                print(f"⚠️ 更新 group 結單狀態失敗: {ge}")

                    group_orders = {}

                    # 3. 讀取訂單紀錄 (優先從 session 專屬資料庫)
                    if target_session_id and 'db' in globals() and db:
                        try:
                            s_orders = db.collection('sessions').document(target_session_id).collection('orders').stream()
                            group_orders = {order.id: order.to_dict() for order in s_orders}
                        except Exception as se:
                            print(f"⚠️ 讀取 session 專屬訂單失敗: {se}")

                    if not group_orders and target_initiator_id and 'db' in globals() and db:
                        try:
                            i_orders = db.collection('groups').document(group_id).collection('initiators').document(target_initiator_id).collection('orders').stream()
                            group_orders = {order.id: order.to_dict() for order in i_orders}
                        except Exception as ie:
                            print(f"⚠️ 讀取 initiator 專屬訂單失敗: {ie}")

                    if not group_orders:
                        reply_text = f"🔒 【{target_initiator_name}】發起的團購已宣告結單！\n目前尚無人點餐，點餐通道已關閉。"
                    else:
                        total_amount = 0
                        order_details = []
                        global_item_summary = {}

                        for user_id, user_order in group_orders.items():
                            user_total = user_order['total']
                            total_amount += user_total
                            shop_name = user_order.get('shop_name', '')
                            if shop_name and not target_shop_name:
                                target_shop_name = shop_name
                            
                            user_name = user_order.get('user_name', '')
                            if not user_name or user_name == 'Unknown':
                                try:
                                    profile = line_bot_api.get_profile(user_id)
                                    user_name = profile.display_name
                                except:
                                    user_name = f"使用者 {user_id[-4:]}"
                            
                            user_items = user_order.get('items', [])
                            items_str_list = []
                            for item in user_items:
                                item_name = item['item']
                                qty = item['quantity']
                                items_str_list.append(f"{item_name}({qty}份)")
                                global_item_summary[item_name] = global_item_summary.get(item_name, 0) + qty

                            order_details.append({
                                'user_name': user_name,
                                'items': "、".join(items_str_list),
                                'subtotal': user_total
                            })
                        
                        reply_text = "🎉 本次團購已成功結單！\n"
                        reply_text += "🔒 點餐通道已關閉，無法再修改或新增訂單\n"
                        reply_text += f"👑 發起人：{target_initiator_name}\n"
                        if target_shop_name:
                            reply_text += f"🏪 店家：{target_shop_name}\n"
                        reply_text += "=" * 28 + "\n"
                        
                        reply_text += "👤 【最終個人點餐明細】\n"
                        for detail in order_details:
                            reply_text += f"• {detail['user_name']}：{detail['items']} (${detail['subtotal']})\n"
                        
                        reply_text += "-" * 28 + "\n"
                        
                        total_items_count = sum(global_item_summary.values())
                        reply_text += "🍱 【全群組餐點總計】\n"
                        for item_name, qty in global_item_summary.items():
                            reply_text += f"• {item_name} × {qty} 份\n"
                        
                        reply_text += "=" * 28 + "\n"
                        reply_text += f"🔢 總餐點數：{total_items_count} 份\n"
                        reply_text += f"💰 總金額：${total_amount}"
                        
                        print(f"🔒 [結單成功] 群組 {group_id} (發起人: {target_initiator_name}) 團購已結單。")

            
            elif real_command == "完成":
                group_id = getattr(event.source, 'group_id', None)
                caller_user_id = event.source.user_id

                if not group_id:
                    reply_text = "❌ 此指令只能在群組中使用"
                else:
                    # 前置檢查：完成前必須先結單
                    group_doc_pre = db.collection('groups').document(group_id).get()
                    is_already_closed = False
                    if group_doc_pre.exists:
                        is_already_closed = group_doc_pre.to_dict().get('is_closed', False)

                    if not is_already_closed:
                        reply_text = (
                            "⚠️ 完成失敗！請先執行「結單」再完成。\n"
                            "操作步驟：結單 → 完成"
                        )
                    else:
                        # 1. 找到當前團購資訊
                        target_initiator_id = None
                        target_initiator_name = "發起人"
                        target_shop_name = ""
                        target_session_id = None

                        caller_init_doc = db.collection('groups').document(group_id).collection('initiators').document(caller_user_id).get()
                        if caller_init_doc.exists:
                            init_info = caller_init_doc.to_dict()
                            target_initiator_id = caller_user_id
                            target_initiator_name = init_info.get('initiator_name', '發起人')
                            target_shop_name = init_info.get('shop_name', '')
                            target_session_id = init_info.get('session_id')
                        else:
                            group_doc = db.collection('groups').document(group_id).get()
                            if group_doc.exists:
                                group_info = group_doc.to_dict()
                                target_initiator_id = group_info.get('initiator_id')
                                target_initiator_name = group_info.get('initiator_name', '發起人')
                                target_shop_name = group_info.get('shop_name', '')
                                target_session_id = group_info.get('session_id')

                        # 2. 讀取所有訂單
                        group_orders = {}
                        if target_session_id and 'db' in globals() and db:
                            try:
                                s_orders = db.collection('sessions').document(target_session_id).collection('orders').stream()
                                group_orders = {o.id: o.to_dict() for o in s_orders}
                            except Exception as se:
                                print(f"⚠️ 讀取 session 訂單失敗: {se}")

                        if not group_orders and target_initiator_id and 'db' in globals() and db:
                            try:
                                i_orders = db.collection('groups').document(group_id).collection('initiators').document(target_initiator_id).collection('orders').stream()
                                group_orders = {o.id: o.to_dict() for o in i_orders}
                            except Exception as ie:
                                print(f"⚠️ 讀取 initiator 訂單失敗: {ie}")

                        if not group_orders:
                            # 無人點餐 → 仍然完成，但不寫任何歷史紀錄
                            if target_session_id:
                                try:
                                    db.collection('sessions').document(target_session_id).set({'completed': True}, merge=True)
                                    print(f"✅ [Session] 無訂單完成，已標記 session {target_session_id}")
                                except Exception as cse:
                                    print(f"⚠️ 標記 session 完成失敗: {cse}")
                            reply_text = f"✅ 本次團購已完成！\n"
                            reply_text += f"👑 發起人：{target_initiator_name}\n"
                            if target_shop_name:
                                reply_text += f"🏪 店家：{target_shop_name}\n"
                            reply_text += "📋 本次團購無人點餐，未產生任何歷史紀錄。"
                            print(f"✅ [完成] 群組 {group_id} 無訂單完成。")

                        else:
                            saved_count = 0
                            total_amount = sum(o.get('total', 0) for o in group_orders.values())

                            # 查詢群組名稱
                            group_name = ''
                            try:
                                group_summary = line_bot_api.get_group_summary(group_id)
                                group_name = group_summary.group_name or ''
                                print(f"📍 群組名稱：{group_name}")
                            except Exception as gne:
                                print(f"⚠️ 無法取得群組名稱: {gne}")

                            # 3. 逐一寫入每位成員的 order_history
                            for uid, user_order in group_orders.items():
                                history_record = {
                                    'group_id': group_id,
                                    'group_name': group_name,
                                    'initiator_id': target_initiator_id,
                                    'initiator_name': target_initiator_name,
                                    'shop_name': target_shop_name or user_order.get('shop_name', ''),
                                    'session_id': target_session_id,
                                    'items': user_order.get('items', []),
                                    'total': user_order.get('total', 0),
                                    'group_total': total_amount,
                                    'user_name': user_order.get('user_name', ''),
                                    'completed_at': firestore.SERVER_TIMESTAMP,
                                }
                                try:
                                    db.collection('users').document(uid).collection('order_history').add(history_record)
                                    saved_count += 1
                                    print(f"📦 [完成] 已寫入使用者 {uid} 的歷史訂單")
                                except Exception as he:
                                    print(f"⚠️ 寫入使用者 {uid} 歷史訂單失敗: {he}")

                            reply_text = f"✅ 本次團購已完成並歸檔！\n"
                            reply_text += f"👑 發起人：{target_initiator_name}\n"
                            if target_shop_name:
                                reply_text += f"🏪 店家：{target_shop_name}\n"
                            reply_text += f"👥 共 {saved_count} 位成員的訂單已存入歷史紀錄\n"
                            reply_text += f"💰 本次團購總金額：${total_amount}\n"
                            reply_text += "📱 成員可在點餐頁面查看個人歷史訂單明細！"
                            print(f"✅ [完成] 群組 {group_id} 團購完成，共寫入 {saved_count} 筆歷史訂單。")

                            # 標記 session 已完成，供取消結單檢查用
                            if target_session_id:
                                try:
                                    db.collection('sessions').document(target_session_id).set({'completed': True}, merge=True)
                                    print(f"✅ [Session] 已標記 session {target_session_id} 為已完成")
                                except Exception as cse:
                                    print(f"⚠️ 標記 session 完成失敗: {cse}")


            elif real_command == "取消結單":
                group_id = getattr(event.source, 'group_id', None)
                caller_user_id = event.source.user_id

                if not group_id:
                    reply_text = "❌ 此指令只能在群組中使用"
                else:
                    # 尋找目前進行中的團購 session
                    target_session_id = None
                    target_initiator_name = "發起人"
                    target_shop_name = ""

                    caller_init_doc = db.collection('groups').document(group_id).collection('initiators').document(caller_user_id).get()
                    if caller_init_doc.exists:
                        init_info = caller_init_doc.to_dict()
                        target_initiator_name = init_info.get('initiator_name', '發起人')
                        target_shop_name = init_info.get('shop_name', '')
                        target_session_id = init_info.get('session_id')
                    else:
                        group_doc = db.collection('groups').document(group_id).get()
                        if group_doc.exists:
                            group_info = group_doc.to_dict()
                            target_initiator_name = group_info.get('initiator_name', '發起人')
                            target_shop_name = group_info.get('shop_name', '')
                            target_session_id = group_info.get('session_id')

                    if not target_session_id:
                        reply_text = "❌ 找不到進行中的團購，無法取消結單"
                    else:
                        # 前置檢查 1：必須在結單後才能取消結單
                        group_doc_check = db.collection('groups').document(group_id).get()
                        is_currently_closed = False
                        if group_doc_check.exists:
                            is_currently_closed = group_doc_check.to_dict().get('is_closed', False)

                        if not is_currently_closed:
                            reply_text = (
                                "⚠️ 取消結單失敗！目前尚未結單，不需要取消。\n"
                                "操作步驟：結單 → (取消結單) → 完成"
                            )
                        else:
                            # 前置檢查 2：完成後不能再取消結單（查 sessions/{id}.completed 標記）
                            already_completed = False
                            try:
                                sess_doc = db.collection('sessions').document(target_session_id).get()
                                if sess_doc.exists:
                                    already_completed = sess_doc.to_dict().get('completed', False)
                            except Exception as ce:
                                print(f"⚠️ 查詢完成狀態失敗: {ce}")

                            if already_completed:
                                reply_text = (
                                    "⚠️ 取消結單失敗！本次團購已執行「完成」並歸檔，\n"
                                    "無法再取消結單。"
                                )
                            else:
                                # 1. 更新記憶體內的 session
                                if target_session_id in menu_sessions and isinstance(menu_sessions[target_session_id], dict):
                                    menu_sessions[target_session_id]['is_closed'] = False

                                # 2. 更新 Firestore sessions 文件
                                try:
                                    db.collection('sessions').document(target_session_id).set({'is_closed': False}, merge=True)
                                except Exception as se:
                                    print(f"⚠️ 更新 session 取消結單失敗: {se}")

                                # 3. 更新 Firestore groups 文件
                                try:
                                    db.collection('groups').document(group_id).set({'is_closed': False}, merge=True)
                                except Exception as ge:
                                    print(f"⚠️ 更新 group 取消結單失敗: {ge}")

                                reply_text = f"🔓 結單已取消！點餐通道重新開放！\n"
                                reply_text += f"👑 發起人：{target_initiator_name}\n"
                                if target_shop_name:
                                    reply_text += f"🏪 店家：{target_shop_name}\n"
                                reply_text += "📝 團購成員可再次修改或新增訂單！"
                                print(f"🔓 [取消結單] 群組 {group_id} 團購取消結單。")


            elif real_command == "清單":
                caller_user_id = event.source.user_id
                try:
                    shops_ref = db.collection('users').document(caller_user_id).collection('shops').stream()
                    shops_list = [doc.id for doc in shops_ref]

                    if not shops_list:
                        reply_text = "📋 您目前尚未儲存任何店家。\n請先使用「@機器人 上傳 店家名稱」來新增店家！"
                    else:
                        total = len(shops_list)
                        show_list = shops_list[:10]
                        reply_text = f"📋 您的店家清單（共 {total} 家）\n"
                        reply_text += "=" * 24 + "\n"
                        for i, name in enumerate(show_list, 1):
                            reply_text += f"{i}. {name}\n"
                        if total > 10:
                            remaining = total - 10
                            reply_text += f"…還有 {remaining} 家（共 {total} 家）"
                    print(f"📋 [清單] 使用者 {caller_user_id} 查詢店家清單，共 {len(shops_list) if shops_list else 0} 家")
                except Exception as le:
                    reply_text = f"❌ 查詢清單失敗：{str(le)}"
                    print(f"⚠️ [清單] 查詢失敗: {le}")

            else:
                reply_text = f"我收到指令了：{real_command}\n(提示：支援指令有「上傳 (店家)」、「團購 (店家)」、「開團」、「清單」、「統整」、「結單」、「取消結單」、「完成」)"

                
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
            )
    except Exception as e:
        print(f"LINE API 錯誤: {e}")

if __name__ == "__main__":
    app.run(port=5000)
