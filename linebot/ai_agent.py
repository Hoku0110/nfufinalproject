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
        return {"menu_items": []}

    # 設定模型與 Prompt
    model = "gemini-3-flash-preview"
    prompt = "請檢視這張餐廳菜單的圖片，幫我辨識出所有的『品項名稱』與對應的『價格』。如果圖片中找不到菜單資訊，請回傳空的 menu_items 陣列。"

    # 設定結構化輸出與參數
    generate_content_config = types.GenerateContentConfig(
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=genai.types.Schema(
            type=genai.types.Type.OBJECT,
            required=["menu_items"],
            properties={
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
            return {"menu_items": []}
        
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
        
        return result_dict

    except Exception as e:
        import traceback
        error_msg = str(e)
        print(f"❌ AI 辨識或解析發生錯誤: {error_msg}")
        print(f"📍 完整錯誤堆疊:")
        print(traceback.format_exc())
        return {"menu_items": []}


# ==========================================
# 測試區塊：只有當你直接執行這個檔案時才會跑
# ==========================================
if __name__ == "__main__":
    # 準備一張測試圖片 (例如 menu.jpg) 放在同一個資料夾
    # 執行這支檔案，看看能不能印出漂亮的字典！
    test_result = extract_menu("menu.jpg")
    print("\n✅ 後端將會收到這樣的資料：")
    print(test_result)
