記得去requirements.txt
去載相關的python套件

cmd: pip install -r requirements.txt

若本機需要 Playwright 內建 Chromium 瀏覽器（做為未安裝 Chrome 時的備援），請再執行一次：
cmd: playwright install chromium

+自己使用.env檔(有一個sample.env可以去更改為自己的api去做使用)
code內使用的.env檔 沒有檔名（放在 `linebot/.env`；若要啟用 foodpanda_order.py 的 AI 思考點擊，請在 `linebot/.env` 或終端機環境變數設定 `OPENAI_API_KEY`）

---

## 如何登入 Playwright 瀏覽器帳號（foodpanda 自動點餐前置作業）

為了保留 foodpanda 登入狀態與常用外送地址，`foodpanda_order.py` 會將瀏覽器設定檔保存於本機（預設路徑：`~/.foodpanda_playwright_profile`，Windows 約為 `C:\Users\<使用者名稱>\.foodpanda_playwright_profile`）。首次使用或帳號登出時請依以下步驟登入：

1. 在專案根目錄打開終端機，執行登入設定模式：
   ```powershell
   python foodpanda_order.py --login
   ```
   （若本機未安裝 Google Chrome，可加上 `--browser chromium`）

2. 系統會自動開啟非無頭視窗的瀏覽器並前往 `https://www.foodpanda.com.tw`，請在該瀏覽器視窗中手動完成以下三個步驟：
   - 通過機器人驗證／安全檢查（若有出現）
   - 登入你的 foodpanda 帳號
   - 在頁面頂部設定並儲存常用外送地址

3. 確認右上角已顯示你的帳號名稱與外送地址後，回到終端機按下 `Enter`，即可保存登入狀態。

4. 之後即可透過以下兩種方式自動點餐（程式會直接沿用剛剛保存的登入帳號與地址，且結束後會保持瀏覽器開啟方便確認與手動付款）：
   - **從 LINE 群組觸發**：啟動 `linebot/app.py` 後，於群組先「`@機器人 結單`」，再輸入「`@機器人 送出訂單`」。
   - **從終端機單獨執行**：
     ```powershell
     python foodpanda_order.py
     ```
     （可加上 `--order-file linebot/output_orders/訂單檔名.json` 指定訂單，`--notify-line` 推播購物車品項至 LINE，`--no-ai` 停用 OpenAI 改用純規則模式）
