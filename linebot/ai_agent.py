import json
from PIL import Image
from google import genai
from google.genai import types
import io

# 初始化客戶端 (從環境變數取得 API Key)
def get_client():
    """取得或初始化 Gemini 客戶端"""
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        raise ValueError("GEMINI_API_KEY 未設定在 .env 檔案中")
    
    return genai.Client(api_key=api_key)


def extract_menu(image_input):

    print(f"🤖 正在辨識菜單，請稍候...")
    
    image = None
    try:
        # 處理不同的輸入類型
        if isinstance(image_input, str):
            # 文件路徑
            print(f"📂 讀取圖片檔案: {image_input}")
            image = Image.open(image_input)
        elif isinstance(image_input, bytes):
            # 二進制數據 (例如從 LINE Blob API 獲取的)
            print(f"📦 從二進制數據讀取圖片，大小: {len(image_input)} bytes")
            image = Image.open(io.BytesIO(image_input))
        elif isinstance(image_input, Image.Image):
            # 已經是 PIL Image 物件
            print(f"🖼️ 使用提供的 PIL Image 物件")
            image = image_input
        else:
            raise TypeError(f"不支援的圖片輸入類型: {type(image_input)}")
        
        # 確保圖片為 RGB 或 RGBA
        if image.mode not in ('RGB', 'RGBA'):
            print(f"🔄 轉換圖片模式: {image.mode} -> RGB")
            image = image.convert('RGB')
        
        print(f"✅ 圖片讀取成功，大小: {image.size}, 模式: {image.mode}")
    
    except Exception as e:
        print(f"❌ 圖片讀取失敗: {e}")
        import traceback
        print(traceback.format_exc())
        return {"shop_name": "", "menu_items": []}

    # 設定模型與 Prompt
    model = "gemini-3-flash-preview"
    prompt = "請檢視這張餐廳菜單的圖片，幫我辨識出店家名稱，以及所有的『品項名稱』與對應的『價格』。請以 JSON 物件輸出，包含 shop_name 與 menu_items 兩個欄位。若無法辨識店家名稱，shop_name 請填空字串。若圖片中找不到菜單資訊，請回傳空的 menu_items 陣列。"

    # 設定結構化輸出與參數
    generate_content_config = types.GenerateContentConfig(
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=genai.types.Schema(
            type=genai.types.Type.OBJECT,
            required=["shop_name", "menu_items"],
            properties={
                "shop_name": genai.types.Schema(
                    type=genai.types.Type.STRING,
                    description="菜單上的店家名稱，若無法辨識則為空字串",
                ),
                "menu_items": genai.types.Schema(
                    type=genai.types.Type.ARRAY,
                    description="菜單上的所有品項列表",
                    items=genai.types.Schema(
                        type=genai.types.Type.OBJECT,
                        required=["item_name", "price"],
                        properties={
                            "item_name": genai.types.Schema(
                                type=genai.types.Type.STRING,
                                description="餐點或飲料的名稱",
                            ),
                            "price": genai.types.Schema(
                                type=genai.types.Type.INTEGER,
                                description="餐點的價格（純數字）",
                            ),
                        },
                    ),
                ),
            },
        ),
    )

    try:
        # 初始化客戶端
        client = get_client()
     
        print(f"📋 嘗試方法 1: 使用結構化輸出...")
        response = client.models.generate_content(
            model=model,
            contents=[prompt, image],
            config=generate_content_config,
        )
        raw_response = response.text
        print(f"✅ 方法 1 成功")
        
        print(f"📥 收到 API 回應 (前 400 字): {raw_response[:400]}")
        
        # 解析 JSON
        response_text = raw_response.strip()
        
        # 移除 markdown 代碼塊
        for marker in ["```json", "```", "```python"]:
            response_text = response_text.replace(marker, "")
        response_text = response_text.strip()
        
        # 找到 JSON 內容
        start_idx = response_text.find('{')
        end_idx = response_text.rfind('}')
        
        if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
            print(f"❌ 無法找到有效的 JSON 結構")
            print(f"原始回應: {response_text[:200]}")
            return {"shop_name": "", "menu_items": []}
        
        json_str = response_text[start_idx:end_idx+1]
        print(f"📋 提取的 JSON (前 200 字): {json_str[:200]}")
        
        result_dict = json.loads(json_str)
        
        menu_count = len(result_dict.get('menu_items', []))
        print(f"✅ 成功辨識 {menu_count} 個菜單品項")
        
        if menu_count > 0:
            for idx, item in enumerate(result_dict.get('menu_items', [])[:3], 1):
                print(f"   {idx}. {item.get('item_name', '?')} - ${item.get('price', '?')}")
            if menu_count > 3:
                print(f"   ... 還有 {menu_count - 3} 項")

        result_dict['shop_name'] = str(result_dict.get('shop_name', '')).strip()
        result_dict['menu_items'] = result_dict.get('menu_items', [])
        
        return result_dict

    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"❌ AI 辨識或解析發生錯誤: {error_msg}")
        print(f"📍 完整錯誤堆疊:")
        print(traceback.format_exc())
        return {"shop_name": "", "menu_items": []}



# ==========================================
# 測試區塊：只有當你直接執行這個檔案時才會跑
# ==========================================
if __name__ == "__main__":
    # 準備一張測試圖片 (例如 menu.jpg) 放在同一個資料夾
    # 執行這支檔案，看看能不能印出漂亮的字典！
    test_result = extract_menu("menu.jpg")
    print("\n✅ 後端將會收到這樣的資料：")
    print(test_result)


def get_voice_client():
    """取得語音點餐專用 Gemini 客戶端（優先使用 GEMINI_VOICE_API_KEY，無效時 fallback 到 GEMINI_API_KEY）"""
    import os
    from dotenv import load_dotenv
    load_dotenv()

    voice_key = os.getenv('GEMINI_VOICE_API_KEY', '').strip()
    main_key  = os.getenv('GEMINI_API_KEY', '').strip()

    # 若 voice_key 是有效 Key（非空且非佔位符），優先使用
    def is_valid(k):
        return bool(k) and not k.startswith('請填入') and not k.startswith('自己的')

    if is_valid(voice_key):
        print(f"🔑 [語音點餐] 使用 GEMINI_VOICE_API_KEY")
        api_key = voice_key
    elif is_valid(main_key):
        print(f"🔑 [語音點餐] GEMINI_VOICE_API_KEY 未設定，fallback 使用 GEMINI_API_KEY")
        api_key = main_key
    else:
        raise ValueError("請在 .env 設定 GEMINI_VOICE_API_KEY 或 GEMINI_API_KEY")

    return genai.Client(api_key=api_key)


def parse_voice_order(voice_text: str, menu_items: list, current_cart: dict = None) -> dict:
    """
    使用 Gemini AI 解析語音點餐文字，支援同時「新增」與「取消特定品項」。

    Args:
        voice_text:   語音辨識轉出的文字
        menu_items:   菜單品項列表 [{item_name, price}, ...]
        current_cart: 目前購物車 {item_name: {price, quantity}, ...}（可選）

    Returns:
        dict 包含：
          - add:          List[{item_name, quantity}] 要新增的品項
          - remove:       List[str] 要從購物車移除的品項名稱
          - unrecognized: List[str] 無法對應到菜單的關鍵字
    """
    print(f"🎙️ [語音點餐] 解析文字：{voice_text}")
    print(f"📋 [語音點餐] 菜單共 {len(menu_items)} 項，購物車：{list((current_cart or {}).keys())}")

    if not voice_text or not voice_text.strip():
        return {"add": [], "remove": [], "unrecognized": []}

    if not menu_items:
        return {"add": [], "remove": [], "unrecognized": [voice_text]}

    # 建立菜單清單
    menu_names = [item.get('item_name') or item.get('name') or '' for item in menu_items]
    menu_names = [n for n in menu_names if n]
    menu_list_str = "\n".join(f"- {name}" for name in menu_names)

    # 建立購物車清單（供 AI 知道哪些可以取消）
    cart_str = "（目前購物車為空）"
    if current_cart:
        cart_lines = [f"- {name} × {info.get('quantity', 1)}"
                      for name, info in current_cart.items() if info.get('quantity', 0) > 0]
        cart_str = "\n".join(cart_lines) if cart_lines else "（目前購物車為空）"

    prompt = f"""你是一個點餐助理，擅長解析口語化的點餐語音。
使用者可能在同一句話中同時「新增餐點」和「取消已點的餐點」，請仔細分辨。

【菜單品項】（品項名稱請完全符合清單中的名稱）：
{menu_list_str}

【目前購物車內容】（只有購物車內的品項才能被取消）：
{cart_str}

【使用者的語音文字】：
{voice_text}

【辨識規則】：

1. 新增意圖（加入購物車）：
   - 關鍵詞：要、加、點、加點、來、來一個、給我、我要
   - 例：「我要珍珠奶茶」→ add: [珍珠奶茶×1]
   - 若未指定數量，預設為 1；「一個/一份/一碗」=1、「兩個/兩份」=2，依此類推

2. 取消意圖（從購物車移除）：
   - 關鍵詞：取消、不要、拿掉、移除、刪掉、退掉、不要了 + 品項名稱
   - 例：「取消鍋燒意麵」→ remove: [鍋燒意麵]
   - 只能取消「目前購物車」中已存在的品項，remove 只填品項名稱（整份移除）

3. 常見語音辨識錯字自動校正（非常重要）：
   - 語音辨識常有同音錯字，請根據上下文與菜單進行「發音相似度」推論
   - 例如：「目標」很可能是「不要」的錯字
   - 例如：「裡面」很可能是「意麵」的錯字
   - 例如：「珍奶」=「珍珠奶茶」、「雞牌」=「雞排」
   - 請聰明地將錯字對應到正確的指令或菜單品項上

4. 混合指令（最重要）：
   - 同一句話可能同時有新增和取消
   - 例：「取消鍋燒意麵鍋燒雞絲 加點紅茶」→ remove:[鍋燒意麵, 鍋燒雞絲], add:[紅茶×1]
   - 例：「不要牛肉麵，改成豬腳麵」→ remove:[牛肉麵], add:[豬腳麵×1]

5. 連續列舉品項：
   - 多個品項可能連續出現沒有分隔，請滑動比對菜單拆解
   - 例：「取消鍋燒意麵鍋燒雞絲」→ 對照菜單 → remove:[鍋燒意麵, 鍋燒雞絲]

6. 只辨識菜單中實際存在的品項，不要自行創造名稱

請以 JSON 格式回覆：
- add: 要新增的品項與數量列表
- remove: 要從購物車移除的品項名稱列表
- unrecognized: 明顯提到但菜單中找不到對應品項的關鍵字（若無則為空陣列）"""

    config = types.GenerateContentConfig(
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=genai.types.Schema(
            type=genai.types.Type.OBJECT,
            required=["add", "remove", "unrecognized"],
            properties={
                "add": genai.types.Schema(
                    type=genai.types.Type.ARRAY,
                    description="要新增到購物車的菜單品項與數量",
                    items=genai.types.Schema(
                        type=genai.types.Type.OBJECT,
                        required=["item_name", "quantity"],
                        properties={
                            "item_name": genai.types.Schema(
                                type=genai.types.Type.STRING,
                                description="菜單上的品項名稱（必須完全符合菜單中的名稱）",
                            ),
                            "quantity": genai.types.Schema(
                                type=genai.types.Type.INTEGER,
                                description="新增數量，最少為 1",
                            ),
                        },
                    ),
                ),
                "remove": genai.types.Schema(
                    type=genai.types.Type.ARRAY,
                    description="要從購物車移除的品項名稱列表（整份移除）",
                    items=genai.types.Schema(
                        type=genai.types.Type.STRING,
                        description="要移除的品項名稱（必須完全符合菜單中的名稱）",
                    ),
                ),
                "unrecognized": genai.types.Schema(
                    type=genai.types.Type.ARRAY,
                    description="使用者明顯提到但菜單中找不到對應品項的關鍵字",
                    items=genai.types.Schema(
                        type=genai.types.Type.STRING,
                    ),
                ),
            },
        ),
    )

    try:
        client = get_voice_client()
        print(f"📤 [語音點餐] Prompt 前 500 字：\n{prompt[:500]}")
        
        models_to_try = ["gemini-3.6-flash", "gemini-1.5-flash", "gemini-2.5-flash", "gemini-2.0-flash-exp"]
        response = None
        last_error = None
        
        for model_name in models_to_try:
            try:
                print(f"🔄 [語音點餐] 嘗試呼叫模型 {model_name}...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=[prompt],
                    config=config,
                )
                print(f"✅ [語音點餐] 模型 {model_name} 呼叫成功")
                break
            except Exception as e:
                print(f"⚠️ [語音點餐] 模型 {model_name} 失敗：{e}")
                last_error = e
                
        if not response:
            raise last_error

        raw = response.text.strip()
        print(f"🤖 [語音點餐] Gemini 完整回應：{raw}")

        for marker in ["```json", "```"]:
            raw = raw.replace(marker, "")
        raw = raw.strip()

        result = json.loads(raw)

        add_items    = [r for r in result.get("add", [])    if r.get("quantity", 0) > 0]
        remove_items = [r for r in result.get("remove", []) if isinstance(r, str) and r]
        unrecognized = result.get("unrecognized", [])

        print(f"✅ [語音點餐] 新增 {len(add_items)} 項，移除 {len(remove_items)} 項，未辨識 {len(unrecognized)} 項")
        for r in add_items:
            print(f"   ＋{r['item_name']} × {r['quantity']}")
        for r in remove_items:
            print(f"   －{r}")

        return {"add": add_items, "remove": remove_items, "unrecognized": unrecognized}

    except Exception as e:
        import traceback
        print(f"❌ [語音點餐] Gemini 解析失敗：{e}")
        print(traceback.format_exc())
        return {"add": [], "remove": [], "unrecognized": [], "error": str(e)}
