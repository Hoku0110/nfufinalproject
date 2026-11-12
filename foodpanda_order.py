"""本機端 Playwright 自動化腳本：讀取訂單 JSON 並於 foodpanda 網頁加入購物車（人工處理驗證與最終付款）。"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ORDERS_DIR = PROJECT_ROOT / "linebot" / "output_orders"
DEFAULT_ENV_PATH = PROJECT_ROOT / "linebot" / ".env"
DEFAULT_PROFILE_DIR = Path.home() / ".foodpanda_playwright_profile"
FOODPANDA_URL = "https://www.foodpanda.com.tw"
DEFAULT_AI_MODEL = "gpt-6-luna"
MAX_AI_REPEAT_COUNT = 3

TOKEN_USAGE: dict[str, int] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "consecutive_errors": 0}


class AIAbortError(RuntimeError):
    """AI 重複思考或無進展達上限時中斷整個程式。"""

BOT_CHALLENGE_KEYWORDS = (
    "just a moment",
    "verify you are human",
    "press & hold",
    "press and hold",
    "access denied",
    "請驗證您是人類",
    "人類驗證",
    "機器人驗證",
    "長按",
)


def load_order_data(orders_dir: Path, order_file: str | None = None) -> dict[str, Any]:
    """讀取指定或最新的訂單 JSON 檔案，並回傳 metadata 與 summary_items。"""
    if order_file:
        target_path = Path(order_file)
        if not target_path.is_absolute():
            target_path = PROJECT_ROOT / target_path
    else:
        json_files = sorted(orders_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not json_files:
            raise FileNotFoundError(f"找不到任何訂單 JSON 檔案：{orders_dir}")
        target_path = json_files[0]

    with target_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    metadata = data.get("metadata", {})
    summary_items = data.get("summary_items", [])
    shop_name = metadata.get("shop_name")
    if not shop_name or not summary_items:
        raise ValueError(f"訂單格式不完整，缺少 shop_name 或 summary_items：{target_path}")

    print(f"📄 已載入訂單：{target_path.name}")
    print(f"🏪 目標店家：{shop_name}")
    for item in summary_items:
        print(f"   - {item.get('item_name')} x {item.get('quantity', 1)}")

    return data


def human_delay(min_sec: float = 0.6, max_sec: float = 1.4) -> None:
    """模擬基本操作節奏，避免頁面元素尚未穩定就點擊。"""
    time.sleep(random.uniform(min_sec, max_sec))


def has_bot_challenge(page: Page) -> bool:
    """只依「可見畫面與實際挑戰元件」判斷是否真的被機器人驗證擋住，避免誤判 HTML 內嵌腳本。"""
    challenge_selectors = (
        "#px-captcha",
        ".px-captcha",
        'iframe[src*="captcha"]',
        'iframe[src*="turnstile"]',
        'iframe[src*="perimeterx"]',
        '[id*="cf-turnstile"]',
    )
    for sel in challenge_selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=300):
                return True
        except Exception:
            continue

    try:
        title = (page.title() or "").lower()
        body_text = (page.locator("body").inner_text(timeout=1500) or "").strip()
    except Exception:
        return False

    if any(k in title for k in ("just a moment", "access denied", "human verification", "attention required")):
        return True

    # 正常搜尋／店家頁面有大量文字；驗證頁通常文字很少且包含明確驗證字眼
    if len(body_text) < 1200:
        lower_body = body_text.lower()
        if any(keyword in lower_body for keyword in BOT_CHALLENGE_KEYWORDS):
            return True

    return False


def pause_for_manual_action(message: str) -> None:
    """暫停自動化流程，交由使用者在非無頭視窗中手動處理後按 Enter 繼續。"""
    print(f"\n⏸️  {message}")
    input("👉 請在開啟的瀏覽器視窗手動處理完成後，回到終端機按 Enter 繼續...")


def ensure_no_bot_challenge(page: Page) -> None:
    """若偵測到真實可見的機器人驗證，暫停等待使用者手動完成並等待頁面跳轉穩定。"""
    for attempt in range(2):
        if not has_bot_challenge(page):
            return
        msg = (
            "偵測到機器人驗證 / 安全檢查畫面，請手動完成驗證。"
            if attempt == 0
            else "畫面似乎仍在驗證中，請確認驗證視窗已消失且頁面載入完成。"
        )
        pause_for_manual_action(msg)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        human_delay(1.2, 1.8)


def find_first_visible(page: Page | Locator, selectors: list[str], timeout_ms: int = 2500) -> Locator | None:
    """依序嘗試多個選擇器，回傳第一個可見元素。"""
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except PlaywrightTimeoutError:
            continue
    return None


def search_and_enter_shop(page: Page, shop_name: str) -> None:
    """前往 foodpanda 首頁，搜尋指定店家並點擊第一個搜尋結果。"""
    print(f"\n🌐 前往 {FOODPANDA_URL} ...")
    page.goto(FOODPANDA_URL, wait_until="domcontentloaded", timeout=60000)
    human_delay(1.5, 2.5)
    ensure_no_bot_challenge(page)

    search_selectors = [
        'input[type="search"]',
        'input[placeholder*="搜尋"]',
        'input[placeholder*="餐廳"]',
        '[data-testid*="search"] input',
        'input[aria-label*="搜尋"]',
    ]

    search_input = find_first_visible(page, search_selectors, timeout_ms=4000)
    if search_input is None:
        pause_for_manual_action(
            "目前畫面找不到頂部搜尋框（可能需要先登入或設定外送地址）。\n"
            "請在瀏覽器設定好外送地址並進入有搜尋框的頁面。"
        )
        ensure_no_bot_challenge(page)
        search_input = find_first_visible(page, search_selectors, timeout_ms=5000)

    if search_input is None:
        raise RuntimeError("仍找不到 foodpanda 頂部搜尋框，請確認頁面狀態。")

    print(f"🔎 搜尋店家：{shop_name}")
    search_input.click()
    human_delay(0.2, 0.4)
    search_input.fill("")
    search_input.press_sequentially(shop_name, delay=80)
    human_delay(0.5, 0.9)
    search_input.press("Enter")
    human_delay(2.0, 3.0)
    ensure_no_bot_challenge(page)

    # 若使用者過驗證時已順手點進店家頁面，直接往下走
    if "/restaurant/" not in page.url:
        vendor_card_selectors = [
            'a[href*="/restaurant/"]',
            '[data-testid*="vendor"] a',
            'li[data-testid*="vendor"]',
            '[class*="vendor-tile"] a',
        ]
        first_vendor = find_first_visible(page, vendor_card_selectors, timeout_ms=6000)
        if first_vendor is None:
            pause_for_manual_action(f"找不到「{shop_name}」的搜尋結果店家卡片，請手動點進該店家頁面。")
        else:
            first_vendor.scroll_into_view_if_needed()
            human_delay(0.3, 0.7)
            first_vendor.click()
            try:
                page.wait_for_url("**/restaurant/**", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            human_delay(1.2, 2.0)
            ensure_no_bot_challenge(page)

    if "/restaurant/" not in page.url:
        pause_for_manual_action(
            f"尚未確認進入店家菜單頁（目前網址：{page.url}）。\n"
            "請在瀏覽器手動點進目標店家頁面後再繼續。"
        )
        ensure_no_bot_challenge(page)

    print(f"✅ 已進入店家頁面：{page.url}")


def get_cart_signature(page: Page) -> str:
    """讀取目前頁面的購物車摘要文字與數量，用來驗證餐點是否真的加入成功。"""
    cart_selectors = [
        '[data-testid*="cart"]',
        'aside',
        '[class*="cart"]',
        'button:has-text("查看購物車")',
        'a:has-text("查看購物車")',
    ]
    snippets: list[str] = []
    for selector in cart_selectors:
        loc = page.locator(selector).first
        try:
            if loc.is_visible(timeout=500):
                text = (loc.inner_text(timeout=800) or "").strip()
                if text:
                    snippets.append(text)
        except Exception:
            continue
    return "\n".join(snippets)


SIZE_HINT_MAP: dict[str, list[str]] = {
    "S": ["小杯", "小", "S"],
    "小": ["小杯", "小", "S"],
    "M": ["中杯", "中", "M"],
    "中": ["中杯", "中", "M"],
    "L": ["大杯", "大", "L"],
    "大": ["大杯", "大", "L"],
    "XL": ["特大杯", "特大", "XL"],
    "冷": ["冷", "冰"],
    "冰": ["冰", "冷"],
    "熱": ["熱", "溫"],
}


def parse_item_spec(item_name: str) -> tuple[list[str], list[str]]:
    """拆解品名、括號別名與規格提示，例如 '1 號 (四季春珍波椰) (M)' -> (['1號', '四季春珍波椰', '1 號'], ['中杯', '中', 'M'])。"""
    hints: list[str] = []
    alt_names: list[str] = []
    tags = re.findall(r"[（(]([^）)]+)[）)]", item_name)
    for tag in tags:
        clean_tag = tag.strip()
        if not clean_tag:
            continue
        key = clean_tag.upper()
        if key in SIZE_HINT_MAP:
            hints.extend(SIZE_HINT_MAP[key])
        else:
            hints.append(clean_tag)
            # 非尺寸／溫度且非純價格標記的括號文字，視為品項別名（例如 1 號 (四季春珍波椰)）
            if not re.fullmatch(r"[\+\-\$\d\s.,元]+", clean_tag):
                alt_names.append(clean_tag)

    base_name = re.sub(r"\s*[（(][^）)]+[）)]", "", item_name).strip()
    search_queries: list[str] = []

    def add_query(q: str) -> None:
        val = q.strip()
        if val and val not in search_queries:
            search_queries.append(val)

    if base_name:
        compact_base = re.sub(r"\s+", "", base_name)
        add_query(compact_base)
    for alt in alt_names:
        add_query(alt)
        add_query(re.sub(r"\s+", "", alt))
    if base_name:
        add_query(base_name)
    if not search_queries:
        add_query(item_name)

    return search_queries, hints


SUGAR_PREFERENCES = ("微糖", "三分糖", "3分糖", "三分")
ICE_PREFERENCES = ("微冰", "去冰")


def inspect_modal_radio_groups(modal: Locator) -> list[dict[str, Any]]:
    """在彈窗 DOM 中依 radio name / 容器自動分組，回傳各選項群組資訊並標記可定位的選擇器。"""
    return modal.evaluate(
        """(modalEl) => {
            modalEl.querySelectorAll('[data-fp-opt-id]').forEach(el => el.removeAttribute('data-fp-opt-id'));

            const radios = Array.from(modalEl.querySelectorAll('input[type="radio"], [role="radio"]'));
            if (radios.length === 0) return [];

            const groupMap = new Map();
            let autoGroupIdx = 0;

            const findGroupContainer = (el) => {
                let cur = el.parentElement;
                let best = cur;
                for (let depth = 0; depth < 7 && cur && cur !== modalEl; depth++) {
                    const txt = (cur.innerText || '').trim();
                    const radioCount = cur.querySelectorAll('input[type="radio"], [role="radio"]').length;
                    if (radioCount >= 1 && txt.length > 0 && txt.length < 1800) {
                        best = cur;
                        if (/必選|必填|Required|選擇\\s*1\\s*個|甜度|糖度|冰塊|冰度|變化|尺寸|容量/.test(txt)) {
                            if (radioCount > 1 || /必選|必填|Required/.test(txt)) {
                                return cur;
                            }
                        }
                    }
                    cur = cur.parentElement;
                }
                return best || el.parentElement;
            };

            const findOptionRow = (radio) => {
                const closestLabel = radio.closest('label');
                if (closestLabel && (closestLabel.innerText || '').trim().length > 0) {
                    return closestLabel;
                }
                if (radio.id) {
                    const forLabel = modalEl.querySelector(`label[for="${CSS.escape(radio.id)}"]`);
                    if (forLabel && (forLabel.innerText || '').trim().length > 0) {
                        return forLabel;
                    }
                }
                // 往上找仍只包含這 1 顆 radio 且有實際文字的整列選項容器
                let bestRow = closestLabel || radio.parentElement || radio;
                let cur = radio.parentElement;
                for (let depth = 0; depth < 5 && cur && cur !== modalEl; depth++) {
                    const count = cur.querySelectorAll('input[type="radio"], [role="radio"]').length;
                    if (count !== 1) break;
                    const txt = (cur.innerText || '').trim();
                    if (txt.length > 0 && txt.length < 120) {
                        bestRow = cur;
                    }
                    cur = cur.parentElement;
                }
                return bestRow;
            };

            radios.forEach((radio) => {
                const name = radio.getAttribute('name');
                const container = findGroupContainer(radio);
                let key = name ? `name:${name}` : '';
                if (!key) {
                    if (!container.dataset.fpGroupTmp) {
                        container.dataset.fpGroupTmp = `g${autoGroupIdx++}`;
                    }
                    key = `dom:${container.dataset.fpGroupTmp}`;
                }
                if (!groupMap.has(key)) {
                    groupMap.set(key, { container, radios: [] });
                }
                groupMap.get(key).radios.push(radio);
            });

            modalEl.querySelectorAll('[data-fp-group-tmp]').forEach(el => el.removeAttribute('data-fp-group-tmp'));

            const results = [];
            let optSeq = 0;
            for (const [key, entry] of groupMap.entries()) {
                const containerText = (entry.container.innerText || '').trim();
                const parentText = (entry.container.parentElement?.innerText || '').trim();
                const options = entry.radios.map((radio) => {
                    const id = `fp-opt-${optSeq++}`;
                    const clickTarget = findOptionRow(radio);
                    clickTarget.setAttribute('data-fp-opt-id', id);
                    const labelText = (
                        clickTarget.innerText ||
                        radio.getAttribute('aria-label') ||
                        radio.value ||
                        ''
                    ).trim();
                    const checked = Boolean(radio.checked || radio.getAttribute('aria-checked') === 'true');
                    return { id, labelText, checked };
                });
                results.push({
                    key,
                    groupText: containerText,
                    contextText: `${containerText}\\n${parentText.slice(0, 600)}`,
                    options,
                });
            }
            return results;
        }"""
    )


def pick_preferred_option_id(
    options: list[dict[str, Any]],
    hints: list[str] | tuple[str, ...],
) -> dict[str, Any] | None:
    """依提示關鍵字優先順序挑出最匹配的選項。"""
    for hint in hints:
        for opt in options:
            if hint in opt.get("labelText", ""):
                return opt
    return None


def select_required_radio_options(modal: Locator, option_hints: list[str] | None = None) -> None:
    """多輪掃描彈窗內所有單選群組：優先選微糖、微冰/去冰，無糖度冰塊則直接跳過，其餘必選點第一項。"""
    clicked_labels: list[str] = []

    for pass_idx in range(6):
        groups = inspect_modal_radio_groups(modal)
        if pass_idx == 0 and groups:
            for g in groups:
                title = (g.get("groupText", "").splitlines()[0] if g.get("groupText") else "").strip()
                opt_names = [o.get("labelText", "").replace("\n", " ").strip() for o in g.get("options", [])]
                print(f"   📋 彈窗群組「{title}」選項：{opt_names}")

        made_click = False

        for group in groups:
            group_text = group.get("groupText", "")
            context_text = group.get("contextText", "")
            options = group.get("options", [])
            if not options:
                continue

            options_joined = " ".join(o.get("labelText", "") for o in options)
            is_sugar_group = any(
                k in group_text or k in options_joined
                for k in ("甜度", "糖度", "糖量", "微糖", "少糖", "半糖", "無糖")
            )
            is_ice_group = any(
                k in group_text or k in options_joined
                for k in ("冰塊", "冰度", "冰量", "微冰", "少冰", "去冰", "完全去冰")
            )
            is_required = any(tag in context_text for tag in ("必選", "必填", "Required", "選擇1個", "選擇 1 個"))

            # 沒有糖度、冰塊且非必選的群組，直接跳過
            if not (is_sugar_group or is_ice_group or is_required):
                continue

            preferred_hints: list[str] | tuple[str, ...] = ()
            if is_sugar_group:
                preferred_hints = SUGAR_PREFERENCES
            elif is_ice_group:
                preferred_hints = ICE_PREFERENCES
            elif option_hints:
                preferred_hints = option_hints

            target_opt = pick_preferred_option_id(options, preferred_hints) if preferred_hints else None
            any_checked = any(opt.get("checked") for opt in options)

            if target_opt is None:
                # 找不到對應糖度/冰塊且不是必選，或已有預設值時，直接跳過不強迫改點正常
                if any_checked or not is_required:
                    continue
                target_opt = options[0]

            if target_opt.get("checked"):
                continue

            opt_locator = modal.locator(f'[data-fp-opt-id="{target_opt["id"]}"]').first
            try:
                opt_locator.scroll_into_view_if_needed()
                human_delay(0.15, 0.3)
                opt_locator.click()
                label_clean = target_opt.get("labelText", "").replace("\n", " ").strip()
                clicked_labels.append(label_clean or "已選項目")
                made_click = True
                human_delay(0.35, 0.6)
                break
            except Exception:
                continue

        if not made_click:
            break

    if clicked_labels:
        print(f"   ☑️ 已選擇規格：{', '.join(clicked_labels)}")


def get_openai_client(
    use_ai: bool = True,
    api_key_override: str | None = None,
    base_url_override: str | None = None,
) -> OpenAI | None:
    """讀取環境變數 / .env 並初始化 OpenAI Client；若停用或缺少金鑰則回傳 None。"""
    if not use_ai:
        return None
    load_dotenv(DEFAULT_ENV_PATH)
    load_dotenv(PROJECT_ROOT / ".env")

    api_key = (api_key_override or os.getenv("OPENAI_API_KEY") or "").strip()
    base_url = (base_url_override or os.getenv("OPENAI_BASE_URL") or "").strip() or None
    if not api_key:
        print(
            "⚠️ 未找到 OPENAI_API_KEY，將改用純規則模式點擊。\n"
            "   若要啟用 OpenAI AI 思考，請在終端機設定 $env:OPENAI_API_KEY=\"sk-...\" 或加上 --openai-api-key。"
        )
        return None

    return OpenAI(api_key=api_key, base_url=base_url)


def print_token_summary(ai_model: str) -> None:
    """印出本次執行的實際 Token 消耗與約略費用。"""
    calls = TOKEN_USAGE["calls"]
    if calls == 0:
        return
    in_tok = TOKEN_USAGE["prompt_tokens"]
    out_tok = TOKEN_USAGE["completion_tokens"]
    total_tok = in_tok + out_tok
    est_usd = (in_tok / 1_000_000) * 0.10 + (out_tok / 1_000_000) * 0.50
    print(
        f"\n📊 OpenAI ({ai_model}) 本次消耗：呼叫 {calls} 次｜"
        f"Input {in_tok:,} + Output {out_tok:,} = 合計 {total_tok:,} tokens"
        f"（依 gpt-6-luna 牌價約 ${est_usd:.5f} 美元）"
    )


def call_openai_vision_json(
    client: OpenAI,
    ai_model: str,
    prompt: str,
    png_bytes: bytes,
) -> dict[str, Any]:
    """呼叫 OpenAI Vision API 並解析回傳的 JSON 物件，同時累計 Token 與連續錯誤次數。"""
    b64_img = base64.b64encode(png_bytes).decode("ascii")
    try:
        response = client.chat.completions.create(
            model=ai_model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}},
                    ],
                }
            ],
        )
        if response.usage is not None:
            TOKEN_USAGE["calls"] += 1
            TOKEN_USAGE["prompt_tokens"] += int(response.usage.prompt_tokens or 0)
            TOKEN_USAGE["completion_tokens"] += int(response.usage.completion_tokens or 0)

        raw_text = (response.choices[0].message.content or "{}").strip()
        for marker in ("```json", "```"):
            raw_text = raw_text.replace(marker, "")
        parsed = json.loads(raw_text.strip())
        TOKEN_USAGE["consecutive_errors"] = 0
        return parsed
    except Exception as exc:
        TOKEN_USAGE["consecutive_errors"] += 1
        if TOKEN_USAGE["consecutive_errors"] >= MAX_AI_REPEAT_COUNT:
            raise AIAbortError(
                f"AI 連續呼叫或解析失敗達 {MAX_AI_REPEAT_COUNT} 次，已自動中斷程式（最後錯誤：{exc}）。"
            ) from exc
        raise


def inspect_modal_for_ai(modal: Locator) -> dict[str, Any]:
    """萃取彈窗內所有單選／複選選項、捲動狀態與送出按鈕狀態，供 AI 決策使用。"""
    return modal.evaluate(
        """(modalEl) => {
            modalEl.querySelectorAll('[data-fp-ai-opt]').forEach(el => el.removeAttribute('data-fp-ai-opt'));

            const inputs = Array.from(
                modalEl.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]')
            );
            const options = [];
            let seq = 0;

            const findRow = (inp) => {
                const closestLabel = inp.closest('label');
                if (closestLabel && (closestLabel.innerText || '').trim()) return closestLabel;
                if (inp.id) {
                    const forLabel = modalEl.querySelector(`label[for="${CSS.escape(inp.id)}"]`);
                    if (forLabel && (forLabel.innerText || '').trim()) return forLabel;
                }
                let best = closestLabel || inp.parentElement || inp;
                let cur = inp.parentElement;
                for (let d = 0; d < 5 && cur && cur !== modalEl; d++) {
                    const cnt = cur.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]').length;
                    if (cnt !== 1) break;
                    const txt = (cur.innerText || '').trim();
                    if (txt.length > 0 && txt.length < 120) best = cur;
                    cur = cur.parentElement;
                }
                return best;
            };

            const findGroupTitle = (inp) => {
                let cur = inp.parentElement;
                for (let d = 0; d < 7 && cur && cur !== modalEl; d++) {
                    const cnt = cur.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]').length;
                    const txt = (cur.innerText || '').trim();
                    if (cnt > 1 || /必選|必填|Required|甜度|糖度|冰塊|冰度|變化|尺寸|加料/.test(txt)) {
                        const firstLines = txt.split('\\n').map(s => s.trim()).filter(Boolean).slice(0, 3).join(' | ');
                        return firstLines.slice(0, 80);
                    }
                    cur = cur.parentElement;
                }
                return '';
            };

            inputs.forEach((inp) => {
                const row = findRow(inp);
                if (!row) return;
                const rect = row.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return;

                const id = `opt-${seq++}`;
                row.setAttribute('data-fp-ai-opt', id);
                const text = (row.innerText || inp.getAttribute('aria-label') || inp.value || '')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const checked = Boolean(inp.checked || inp.getAttribute('aria-checked') === 'true');
                const kind = inp.getAttribute('type') || inp.getAttribute('role') || 'option';
                const group = findGroupTitle(inp);
                options.push({ id, group, kind, text, checked });
            });

            // 檢查彈窗是否還可以往下捲動
            let canScrollDown = false;
            const allNodes = [modalEl, ...Array.from(modalEl.querySelectorAll('*'))];
            for (const el of allNodes) {
                const st = window.getComputedStyle(el);
                if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 20) {
                    if (el.scrollTop + el.clientHeight < el.scrollHeight - 12) {
                        canScrollDown = true;
                        break;
                    }
                }
            }

            // 檢查底部送出按鈕狀態
            const btns = Array.from(modalEl.querySelectorAll('button'));
            const submitBtn = btns.find(b => /放入購物車|加入購物車|更新購物車/.test((b.innerText || '').trim()));
            const submitEnabled = Boolean(submitBtn && !submitBtn.disabled);
            const submitText = submitBtn ? (submitBtn.innerText || '').trim() : '';

            return { options, canScrollDown, submitEnabled, submitText };
        }"""
    )


def scroll_modal_down(modal: Locator, pixels: int = 360) -> bool:
    """將彈窗內可捲動區塊往下捲動一段距離，讓下方甜度/冰塊選項顯現。"""
    return modal.evaluate(
        """(modalEl, px) => {
            const allNodes = [modalEl, ...Array.from(modalEl.querySelectorAll('*'))];
            for (const el of allNodes) {
                const st = window.getComputedStyle(el);
                if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 20) {
                    const before = el.scrollTop;
                    el.scrollTop = Math.min(el.scrollHeight, before + px);
                    return el.scrollTop > before;
                }
            }
            return false;
        }""",
        pixels,
    )


def ai_decide_modal_action(
    client: OpenAI,
    ai_model: str,
    modal_png: bytes,
    modal_state: dict[str, Any],
    item_name: str,
    scrolled_once: bool,
) -> dict[str, Any]:
    """呼叫 OpenAI Vision 結合彈窗截圖與選項清單，決定下一步要點擊的選項或動作。"""
    options_json = json.dumps(modal_state.get("options", []), ensure_ascii=False)
    prompt = f"""你正在協助操作 foodpanda 餐點規格彈窗。
目標點餐品項：{item_name}

請根據附上的彈窗截圖與下方可用選項清單，嚴格遵守以下規則決定下一步：
1. 若目標品項有括號規格（如 (M)/(中)、(L)/(大)、(冷)/(熱)），在「變化/尺寸/杯型」中選擇對應選項；若無指定但該必選群組未選，選第一個合理選項。
2. 若有「甜度 / 糖度」選項：**一律選「微糖」**（若無微糖才選三分糖/3分糖）；**若整個彈窗沒有任何甜度選項，直接跳過**。
3. 若有「冰塊 / 冰度 / 溫度」選項：**一律選「微冰」**，沒有微冰就選**「去冰」**；**若整個彈窗沒有任何冰塊選項，直接跳過**。
4. 其他標示「必選 / 必填」且尚未勾選 (`checked: false`) 的群組，選第一個選項；非必選的加料/備註一律不要點。
5. `target_ids` 只能填目前 `checked: false` 且需要新點擊的選項 id（例如 ["opt-1", "opt-4"]）。
6. 注意截圖下方可能還有選項被捲動隱藏：如果 `canScrollDown` 為 true，且還沒看到甜度或冰塊選項（或 `scrolled_once` 為 false 且不確定下方是否還有必選/甜度/冰塊），可以先回傳 `action: "scroll_down"` 往下捲動確認。
7. 當所有必選項目、以及存在的甜度(微糖)/冰塊(微冰或去冰)都已選好，且 `submitEnabled` 為 true 時，回傳 `action: "submit"`。

請只輸出單一 JSON 物件，欄位格式如下：
{{
  "action": "click_options" | "scroll_down" | "submit" | "manual",
  "target_ids": ["opt-0"],
  "reason": "簡短說明理由"
}}

目前狀態：
- canScrollDown: {modal_state.get("canScrollDown")}
- scrolled_once: {scrolled_once}
- submitEnabled: {modal_state.get("submitEnabled")} ({modal_state.get("submitText")})
- options: {options_json}
"""
    return call_openai_vision_json(client, ai_model, prompt, modal_png)


def ai_handle_modal(
    modal: Locator,
    client: OpenAI,
    ai_model: str,
    item_name: str,
) -> bool:
    """使用 OpenAI 視覺＋DOM 多輪思考並點擊彈窗選項；若重複或無進展達 3 次則立即中斷程式。"""
    scrolled_once = False
    last_sig: tuple[str, tuple[str, ...]] | None = None
    repeat_count = 0
    no_progress_count = 0

    for round_idx in range(6):
        modal_state = inspect_modal_for_ai(modal)
        before_checked = tuple(opt["id"] for opt in modal_state.get("options", []) if opt.get("checked"))

        try:
            modal_png = modal.screenshot(type="png")
            decision = ai_decide_modal_action(
                client=client,
                ai_model=ai_model,
                modal_png=modal_png,
                modal_state=modal_state,
                item_name=item_name,
                scrolled_once=scrolled_once,
            )
        except AIAbortError:
            raise
        except Exception as exc:
            print(f"   ⚠️ AI 決策失敗（{exc}），改用規則模式處理彈窗...")
            return False

        action = decision.get("action", "manual")
        target_ids = [str(t) for t in (decision.get("target_ids") or [])]
        reason = decision.get("reason", "")
        print(f"   🤖 AI 第 {round_idx + 1} 輪思考：{reason} -> {action} {target_ids}")

        sig = (action, tuple(sorted(target_ids)))
        if sig == last_sig:
            repeat_count += 1
        else:
            last_sig = sig
            repeat_count = 1

        if repeat_count >= MAX_AI_REPEAT_COUNT:
            raise AIAbortError(
                f"AI 在「{item_name}」連續 {MAX_AI_REPEAT_COUNT} 次做出相同決策（{action} {target_ids}），"
                "已自動中斷程式避免重複消耗 Token。"
            )

        if action == "click_options" and target_ids:
            for tid in target_ids:
                opt_loc = modal.locator(f'[data-fp-ai-opt="{tid}"]').first
                try:
                    opt_loc.scroll_into_view_if_needed()
                    human_delay(0.15, 0.3)
                    opt_loc.click()
                    human_delay(0.35, 0.55)
                except Exception as click_err:
                    print(f"   ⚠️ 點擊 {tid} 失敗：{click_err}")

            after_state = inspect_modal_for_ai(modal)
            after_checked = tuple(opt["id"] for opt in after_state.get("options", []) if opt.get("checked"))
            if after_checked == before_checked:
                no_progress_count += 1
                if no_progress_count >= MAX_AI_REPEAT_COUNT:
                    raise AIAbortError(
                        f"AI 在「{item_name}」連續 {MAX_AI_REPEAT_COUNT} 次點擊選項後狀態皆未改變，"
                        "已自動中斷程式。"
                    )
            else:
                no_progress_count = 0
            continue

        if action == "scroll_down":
            moved = scroll_modal_down(modal)
            scrolled_once = True
            human_delay(0.35, 0.55)
            if not moved:
                no_progress_count += 1
                if modal_state.get("submitEnabled"):
                    action = "submit"
                elif no_progress_count >= MAX_AI_REPEAT_COUNT:
                    raise AIAbortError(
                        f"AI 在「{item_name}」連續 {MAX_AI_REPEAT_COUNT} 次嘗試捲動但無法推進，已自動中斷程式。"
                    )
                else:
                    continue
            else:
                no_progress_count = 0
                continue

        if action == "submit":
            add_btn = find_first_visible(
                modal,
                [
                    'button:has-text("放入購物車")',
                    'button:has-text("加入購物車")',
                    'button:has-text("更新購物車")',
                    'button:has-text("加入")',
                ],
                timeout_ms=2500,
            )
            if add_btn is not None and not add_btn.is_disabled():
                add_btn.scroll_into_view_if_needed()
                human_delay(0.2, 0.4)
                add_btn.click()
                try:
                    modal.wait_for(state="hidden", timeout=4000)
                except PlaywrightTimeoutError:
                    raise AIAbortError(f"「{item_name}」點擊送出後彈窗未關閉，已自動中斷程式。")
                human_delay(0.5, 0.9)
                return True

            no_progress_count += 1
            print("   ⚠️ 送出按鈕尚未啟用，繼續由 AI 檢查剩餘必填項目...")
            if no_progress_count >= MAX_AI_REPEAT_COUNT:
                raise AIAbortError(
                    f"「{item_name}」送出按鈕連續 {MAX_AI_REPEAT_COUNT} 次無法啟用，已自動中斷程式。"
                )
            continue

        break

    raise AIAbortError(f"AI 處理「{item_name}」超過思考輪數上限（6 輪）仍未完成，已自動中斷程式。")


def ai_pick_dish_card_targets(
    page: Page,
    client: OpenAI,
    ai_model: str,
    item_name: str,
    base_name: str,
) -> list[Locator]:
    """掃描畫面上可見的餐點卡片並請 OpenAI 判斷要點哪一張，找不到時回傳空陣列交由規則備援。"""
    cards = page.evaluate(
        """() => {
            document.querySelectorAll('[data-fp-ai-dish]').forEach(el => el.removeAttribute('data-fp-ai-dish'));
            const isVisible = (el) => {
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const isExcluded = (el) => Boolean(
                el.closest('aside, header, nav, [role="dialog"], [data-testid*="cart"], input, textarea, script, style')
            );
            const all = Array.from(document.body.querySelectorAll('div, li, article, button'));
            const picked = [];
            for (const el of all) {
                if (isExcluded(el) || !isVisible(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 150 || r.width > 560 || r.height < 60 || r.height > 360) continue;
                const txt = (el.innerText || '').trim();
                if (!txt || !txt.includes('$') || txt.length > 360) continue;
                if (picked.some(p => el.contains(p.el) || p.el.contains(el))) continue;
                picked.push({ el, txt, top: r.top, left: r.left });
            }
            picked.sort((a, b) => (Math.abs(a.top - b.top) > 20 ? a.top - b.top : a.left - b.left));
            return picked.slice(0, 10).map((item, idx) => {
                const id = `dish-${idx}`;
                item.el.setAttribute('data-fp-ai-dish', id);
                return { id, text: item.txt.replace(/\\s+/g, ' ').trim() };
            });
        }"""
    )
    if not cards:
        return []

    if not any(base_name in c["text"] for c in cards):
        return []

    prompt = f"""請從下列 foodpanda 菜單卡片中，選出最符合「{item_name}」（主品名：{base_name}）且順序最前面的第一張卡片。
候選卡片清單：{json.dumps(cards, ensure_ascii=False)}
若有多張同名卡片，優先選擇畫面最上方/左邊的第一張；若完全沒有符合品項，chosen_id 請回傳空字串 ""。
請只輸出 JSON 物件：{{"chosen_id": "dish-0", "reason": "簡短理由"}}"""

    try:
        page_png = page.screenshot(type="png")
        res = call_openai_vision_json(client, ai_model, prompt, page_png)
        chosen_id = (res.get("chosen_id") or "").strip()
        if chosen_id:
            print(f"   🤖 AI 選卡：{chosen_id}（{res.get('reason', '')}）")
            card_loc = page.locator(f'[data-fp-ai-dish="{chosen_id}"]').first
            btn_loc = card_loc.locator("button, [role='button']").last
            targets: list[Locator] = []
            if btn_loc.count() > 0:
                targets.append(btn_loc)
            targets.append(card_loc)
            return targets
    except AIAbortError:
        raise
    except Exception as exc:
        print(f"   ⚠️ AI 選卡失敗（{exc}），改用規則選卡...")
    return []


def handle_item_modal_if_open(
    page: Page,
    option_hints: list[str] | None = None,
    ai_client: OpenAI | None = None,
    ai_model: str = DEFAULT_AI_MODEL,
    item_name: str = "",
) -> bool:
    """若點擊餐點後跳出規格選擇彈窗，處理必選項目並點擊放入/加入購物車。回傳是否透過彈窗成功加入。"""
    modal = find_first_visible(
        page,
        [
            '[role="dialog"]',
            '[data-testid*="product-modal"]',
            '[data-testid*="item-modal"]',
        ],
        timeout_ms=3500,
    )
    if modal is None:
        return False

    if ai_client is not None:
        if ai_handle_modal(modal, ai_client, ai_model, item_name):
            return True

    select_required_radio_options(modal, option_hints=option_hints)

    add_btn = find_first_visible(
        modal,
        [
            'button:has-text("放入購物車")',
            'button:has-text("加入購物車")',
            'button:has-text("更新購物車")',
            'button:has-text("加入")',
            '[data-testid*="add-to-cart"]',
            'button[type="submit"]',
        ],
        timeout_ms=3000,
    )
    if add_btn is None:
        pause_for_manual_action("彈窗中有無法自動處理的選項或找不到「放入購物車」按鈕，請手動加入該品項。")
        return True

    if add_btn.is_disabled():
        # 再嘗試一次掃描，防止動態載入延遲
        human_delay(0.5, 0.8)
        select_required_radio_options(modal, option_hints=option_hints)

    if add_btn.is_disabled():
        pause_for_manual_action(
            "「放入購物車」按鈕仍為無法點擊狀態（可能有尚未選取的必選／複選項目），\n"
            "請在瀏覽器手動選完並按下「放入購物車」後，按 Enter 繼續。"
        )
        return True

    add_btn.scroll_into_view_if_needed()
    human_delay(0.2, 0.5)
    add_btn.click()
    try:
        modal.wait_for(state="hidden", timeout=4000)
    except PlaywrightTimeoutError:
        pause_for_manual_action("點擊「放入購物車」後彈窗未關閉（可能有未選的必填項目），請在瀏覽器手動確認並加入。")
    human_delay(0.6, 1.0)
    return True


def find_menu_item_targets(page: Page, item_name: str, search_queries: list[str]) -> list[Locator]:
    """在店家菜單搜尋結果區塊內（嚴格鎖定搜尋結果與當前可視範圍、不捲動）定位第一張餐點卡片。"""
    queries: list[str] = []
    for q in [item_name, *search_queries]:
        val = q.strip()
        if val and val not in queries:
            queries.append(val)
    marked = page.evaluate(
        """(queries) => {
            document.querySelectorAll('[data-fp-dish-target]').forEach(el => el.removeAttribute('data-fp-dish-target'));

            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };

            const isExcluded = (el) => Boolean(
                el.closest('aside, header, nav, [role="dialog"], [data-testid*="cart"], input, textarea, script, style')
            );

            const allEls = Array.from(document.body.querySelectorAll('*'));

            // 1. 先定位店內搜尋結果標題（例如 "We found 2 results for ..."）
            let resultsTopMin = 120;
            const resultsBottomMax = window.innerHeight - 15;
            for (const el of allEls) {
                if (isExcluded(el) || !isVisible(el)) continue;
                if (el.children.length > 3) continue;
                const txt = (el.innerText || '').trim();
                if (!txt || txt.length > 100) continue;
                const lower = txt.toLowerCase();
                if (lower.includes('results for') || lower.includes('搜尋結果')) {
                    if (/\\b0\\s*results?\\b/.test(lower) || /(?:^|[^\\d])0\\s*筆/.test(txt)) {
                        return [];
                    }
                    const rect = el.getBoundingClientRect();
                    if (rect.top >= 60 && rect.top <= window.innerHeight - 100) {
                        resultsTopMin = Math.max(120, rect.bottom);
                        break;
                    }
                }
            }

            const candidates = [];
            for (const q of queries) {
                const cleanQ = q.replace(/\\s+/g, '');
                for (const el of allEls) {
                    if (isExcluded(el) || !isVisible(el)) continue;
                    // 只看葉節點或文字精簡的標題元素，避免選到整塊大容器
                    if (el.children.length > 2) continue;
                    const txt = (el.innerText || '').trim();
                    if (!txt || txt.length > 80) continue;
                    const lower = txt.toLowerCase();
                    if (lower.includes('results for') || lower.includes('found ') || txt.includes('搜尋結果')) continue;

                    const rect = el.getBoundingClientRect();
                    // 嚴格限定在搜尋結果區域與當前可視視窗內，絕不點到下方分類卡片
                    if (rect.top < resultsTopMin || rect.bottom > resultsBottomMax) continue;

                    const cleanTxt = txt.replace(/\\s+/g, '');
                    if (txt === q || txt.includes(q) || (cleanQ && cleanTxt.includes(cleanQ))) {
                        candidates.push({ el, exact: txt === q || cleanTxt === cleanQ, top: rect.top, left: rect.left });
                    }
                }
                if (candidates.length > 0) break;
            }

            if (candidates.length === 0) return [];

            // 依畫面由上到下、由左到右排序，只取搜尋結果第一張卡片
            candidates.sort((a, b) => {
                if (Math.abs(a.top - b.top) > 25) return a.top - b.top;
                return a.left - b.left;
            });

            const targetIds = [];
            let seq = 0;
            const addMark = (el) => {
                if (!el || !isVisible(el)) return;
                const rect = el.getBoundingClientRect();
                if (rect.top < resultsTopMin || rect.bottom > resultsBottomMax) return;
                if (!el.getAttribute('data-fp-dish-target')) {
                    const id = `dish-${seq++}`;
                    el.setAttribute('data-fp-dish-target', id);
                    targetIds.push(id);
                }
            };

            const firstCandidate = candidates[0].el;
            // 往上找「單一餐點卡片」外框（含 1 個 $、單張卡片尺寸，絕不擴張到雙欄父容器）
            let card = null;
            let cur = firstCandidate;
            for (let depth = 0; depth < 7 && cur && cur !== document.body; depth++) {
                if (isExcluded(cur)) break;
                const rect = cur.getBoundingClientRect();
                const txt = (cur.innerText || '').trim();
                const dollarCount = (txt.match(/\\$/g) || []).length;
                if (rect.width > 460 || rect.height > 230 || dollarCount > 2) {
                    break;
                }
                if (
                    dollarCount >= 1 &&
                    rect.width >= 150 && rect.width <= 460 &&
                    rect.height >= 50 && rect.height <= 230 &&
                    rect.top >= resultsTopMin && rect.bottom <= resultsBottomMax
                ) {
                    card = cur;
                }
                cur = cur.parentElement;
            }

            if (card) {
                addMark(card);
                const btns = Array.from(card.querySelectorAll('button, [role="button"]')).filter(isVisible);
                if (btns.length === 1) {
                    addMark(btns[0]);
                }
            }
            addMark(firstCandidate);
            return targetIds;
        }""",
        queries,
    )

    return [page.locator(f'[data-fp-dish-target="{tid}"]').first for tid in marked]


def clear_input_field(field: Locator) -> None:
    """確實清空 React / 受控搜尋框的內容。"""
    try:
        field.click(timeout=2000)
        field.press("Control+a")
        field.press("Backspace")
        field.fill("")
    except Exception:
        pass


def wait_for_menu_search_results(page: Page, query: str, timeout_s: float = 4.0) -> str:
    """等待店內搜尋結果標題與第一張符合品名卡片渲染完成；若明確為 0 筆結果則立即回傳 'empty'。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = page.evaluate(
            """(q) => {
                const cleanQ = q.replace(/\\s+/g, '');
                const els = Array.from(document.body.querySelectorAll('h1, h2, h3, h4, p, div, span'));
                let headingTop = 0;
                for (const el of els) {
                    if (el.children.length > 3) continue;
                    const txt = (el.innerText || '').trim();
                    if (!txt || txt.length > 100) continue;
                    const lower = txt.toLowerCase();
                    const cleanTxt = txt.replace(/\\s+/g, '');
                    if ((lower.includes('results for') || txt.includes('搜尋結果')) && (txt.includes(q) || (cleanQ && cleanTxt.includes(cleanQ)))) {
                        if (/\\b0\\s*results?\\b/.test(lower) || /(?:^|[^\\d])0\\s*筆/.test(txt)) {
                            return 'empty';
                        }
                        const r = el.getBoundingClientRect();
                        if (r.height > 0 && r.top >= 60) {
                            headingTop = r.bottom;
                            break;
                        }
                    }
                }
                if (!headingTop) return 'pending';
                for (const el of els) {
                    if (el.closest('aside, header, nav, [role="dialog"], input')) continue;
                    if (el.children.length > 2) continue;
                    const txt = (el.innerText || '').trim();
                    if (!txt || txt.length > 80) continue;
                    if (txt.toLowerCase().includes('results for')) continue;
                    const cleanTxt = txt.replace(/\\s+/g, '');
                    const r = el.getBoundingClientRect();
                    if (r.height > 0 && r.top >= headingTop && r.bottom <= window.innerHeight - 20 && (txt.includes(q) || (cleanQ && cleanTxt.includes(cleanQ)))) {
                        return 'ready';
                    }
                }
                return 'pending';
            }""",
            query,
        )
        if status == "ready":
            human_delay(0.35, 0.55)
            return "ready"
        if status == "empty":
            human_delay(0.15, 0.25)
            return "empty"
        time.sleep(0.2)
    return "timeout"


def click_target_reliably(
    page: Page,
    target: Locator,
    idx: int,
    allow_scroll: bool = True,
) -> bool:
    """點擊目標元件；搜尋結果卡片模式下不捲動頁面，直接以滑鼠座標點擊第一個可視卡片。"""
    try:
        if allow_scroll:
            need_scroll = target.evaluate(
                """el => {
                    const r = el.getBoundingClientRect();
                    return r.top < 150 || r.bottom > window.innerHeight - 30;
                }"""
            )
            if need_scroll:
                target.evaluate("el => el.scrollIntoView({ block: 'center', inline: 'center' })")
                human_delay(0.25, 0.45)

        box = target.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            print(f"   ⚠️ 候選目標 #{idx + 1} 無有效可視座標")
            return False

        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        preview = ""
        try:
            preview = (target.inner_text(timeout=500) or "").splitlines()[0].strip()[:30]
        except Exception:
            preview = ""
        label_info = f"「{preview}」" if preview else ""
        print(
            f"   🖱️ 直接點擊第一個餐點卡片目標 #{idx + 1}{label_info}"
            f"（座標 {int(cx)}, {int(cy)}，尺寸 {int(box['width'])}x{int(box['height'])}）"
        )

        if not allow_scroll:
            page.mouse.move(cx, cy)
            human_delay(0.06, 0.14)
            page.mouse.click(cx, cy)
            return True

        try:
            target.click(timeout=1200)
            return True
        except Exception as click_err:
            print(f"   ↪️ 一般點擊受阻（{type(click_err).__name__}），改用滑鼠座標點擊...")
            page.mouse.move(cx, cy)
            human_delay(0.08, 0.18)
            page.mouse.click(cx, cy)
            return True
    except Exception as exc:
        print(f"   ⚠️ 點擊目標 #{idx + 1} 失敗：{exc}")
        return False


def add_menu_items(
    page: Page,
    summary_items: list[dict[str, Any]],
    ai_client: OpenAI | None = None,
    ai_model: str = DEFAULT_AI_MODEL,
) -> None:
    """依序將 summary_items 加入購物車，並驗證每次是否真的加入成功。"""
    print("\n🍽️  開始選購餐點...")

    # 限定只找店家菜單區域內的搜尋框，避免誤觸頁面頂部的全站搜尋框
    menu_search_selectors = [
        'main input[placeholder*="搜尋菜單"]',
        'main input[placeholder*="店內搜尋"]',
        'main input[placeholder*="搜尋品項"]',
        'main input[type="search"]',
        'input[data-testid*="menu-search"]',
    ]

    for entry in summary_items:
        item_name = str(entry.get("item_name", "")).strip()
        quantity = int(entry.get("quantity", 1))
        if not item_name or quantity <= 0:
            continue

        # 開始新一個品項前，確保沒有未關閉的規格彈窗擋住搜尋欄
        open_modal = find_first_visible(page, ['[role="dialog"]'], timeout_ms=300)
        if open_modal is not None:
            handle_item_modal_if_open(
                page,
                option_hints=None,
                ai_client=ai_client,
                ai_model=ai_model,
                item_name=item_name,
            )

        search_queries, option_hints = parse_item_spec(item_name)
        print(f"➕ 準備加入：{item_name} x {quantity}（搜尋關鍵字：{search_queries}）")
        menu_search = find_first_visible(page, menu_search_selectors, timeout_ms=1000)

        for idx in range(quantity):
            ensure_no_bot_challenge(page)
            targets: list[Locator] = []
            if menu_search is not None:
                # 若為第一份，或前次未成功保留搜尋結果，依序嘗試所有搜尋關鍵字（例如「1號」與「四季春珍波椰」）
                if idx == 0:
                    for q_idx, query in enumerate(search_queries):
                        if q_idx > 0:
                            print(f"   ↪️ 改以別名關鍵字重新搜尋：「{query}」")
                        clear_input_field(menu_search)
                        human_delay(0.25, 0.4)
                        menu_search.press_sequentially(query, delay=50)
                        status = wait_for_menu_search_results(page, query, timeout_s=4.0)
                        if status == "empty":
                            continue
                        targets = find_menu_item_targets(page, item_name, [query, *search_queries])
                        if targets:
                            break
                else:
                    targets = find_menu_item_targets(page, item_name, search_queries)
            else:
                targets = find_menu_item_targets(page, item_name, search_queries)

            if not targets:
                human_delay(0.8, 1.1)
                targets = find_menu_item_targets(page, item_name, search_queries)
            if not targets:
                pause_for_manual_action(
                    f"❌ 菜單中找不到餐點卡片「{item_name}」（第 {idx + 1}/{quantity} 份）。\n"
                    "請在瀏覽器手動搜尋並加入該餐點後，按 Enter 繼續。"
                )
                continue

            added = False
            for t_idx, target in enumerate(targets):
                cart_before = get_cart_signature(page)
                if not click_target_reliably(page, target, t_idx, allow_scroll=False):
                    continue

                modal_added = handle_item_modal_if_open(
                    page,
                    option_hints=option_hints,
                    ai_client=ai_client,
                    ai_model=ai_model,
                    item_name=item_name,
                )
                human_delay(0.5, 0.9)
                cart_after = get_cart_signature(page)

                if modal_added or (cart_after and cart_after != cart_before):
                    added = True
                    break

            if added:
                print(f"   ✓ 已確認加入第 {idx + 1}/{quantity} 份")
            else:
                pause_for_manual_action(
                    f"⚠️ 點擊「{item_name}」後沒有彈出規格視窗，且購物車內容沒有變化（可能沒點到加購按鈕或商品已售完）。\n"
                    "請在瀏覽器確認並手動加入第 "
                    f"{idx + 1}/{quantity} 份後，按 Enter 繼續。"
                )

        if menu_search is not None:
            clear_input_field(menu_search)
            human_delay(0.3, 0.5)


def find_clickable_in_viewport(page: Page, selectors: list[str], timeout_ms: int = 1500) -> Locator | None:
    """找出實際位於目前可視視窗內且可點擊的元素，避開桌機版隱藏在視窗外的手機版黏性按鈕。"""
    vp = page.viewport_size
    if not vp:
        try:
            vp = page.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight })")
        except Exception:
            vp = {"width": 1280, "height": 800}
    for selector in selectors:
        loc_all = page.locator(selector)
        try:
            count = min(loc_all.count(), 4)
        except Exception:
            continue
        for idx in range(count):
            loc = loc_all.nth(idx)
            try:
                loc.wait_for(state="visible", timeout=timeout_ms)
                box = loc.bounding_box()
                if not box or box["width"] <= 0 or box["height"] <= 0:
                    continue
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                if 0 < cx < vp["width"] and 0 < cy < vp["height"]:
                    return loc
            except Exception:
                continue
    return None


def ai_inspect_cart(
    client: OpenAI,
    ai_model: str,
    cart_png: bytes,
    raw_text: str,
) -> tuple[list[str], str]:
    """請 OpenAI 視覺＋文字直接檢視目前購物車內實際有哪些品項、規格、數量與總計金額。"""
    prompt = f"""請直接看這張 foodpanda 購物車 / 結帳畫面截圖，並搭配下方擷取文字，列出「目前購物車裡實際已經加入的餐點品項」。

重要規則：
1. 只列出購物車裡「已加入」的餐點品項（例如：`茉莉綠茶`、`阿薩姆紅茶` 等）。
2. 嚴格排除「加購其他商品 / 加購推薦區」的商品、外送方式（「外送」、「外帶自取」、「尚未提供服務」）、小計、外送費、服務費、平台費、折扣與按鈕文字。
3. 請如實列出目前購物車有的所有品項與數量／已選規格／價格；若購物車是空的，`items` 請回傳空陣列 []。
4. 請只輸出 JSON 物件：
{{
  "items": [
    {{"name": "品項名稱", "spec": "已選規格（如無可留空字串）", "quantity": 1, "price": "$35"}}
  ],
  "total_amount": "$61"
}}

畫面擷取文字參考：
{raw_text[:4000]}
"""
    data = call_openai_vision_json(client, ai_model, prompt, cart_png)
    formatted: list[str] = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        spec = str(item.get("spec") or "").strip()
        qty = item.get("quantity", 1)
        price = str(item.get("price") or "").strip()
        spec_part = f"（{spec}）" if spec else ""
        price_part = f" - {price}" if price else ""
        formatted.append(f"{name}{spec_part} x{qty}{price_part}")

    total = str(data.get("total_amount") or "").strip()
    return formatted, total


def view_cart_and_extract(
    page: Page,
    ai_client: OpenAI | None = None,
    ai_model: str = DEFAULT_AI_MODEL,
) -> tuple[list[str], str]:
    """開啟購物車／結帳確認頁或讀取右側購物車，直接檢視目前購物車實際有哪些品項與總計金額。"""
    print("\n🛒 準備開啟購物車 / 檢視目前購物車品項...")

    # 若畫面仍有殘留規格彈窗，提示先確認再讀取購物車
    leftover_modal = find_first_visible(page, ['[role="dialog"]'], timeout_ms=400)
    if leftover_modal is not None:
        pause_for_manual_action("畫面上仍有未關閉的餐點規格視窗，請確認加入或關閉後按 Enter 繼續。")

    cart_btn = find_clickable_in_viewport(
        page,
        [
            'aside button:has-text("查看付款方式及地址")',
            'button:has-text("查看付款方式及地址")',
            'aside button:has-text("前往結帳")',
            'button:has-text("前往結帳")',
            'button:has-text("查看購物車"):not(.sticky-cart-button)',
            'button:has-text("查看購物車")',
            'a:has-text("查看購物車")',
            '[data-testid*="cart"] button',
        ],
        timeout_ms=1500,
    )

    if cart_btn is not None:
        try:
            cart_btn.click(timeout=5000)
            human_delay(1.8, 2.8)
            ensure_no_bot_challenge(page)
        except Exception as exc:
            print(f"   ⚠️ 點擊購物車按鈕未完成（{type(exc).__name__}），改直接讀取右側購物車明細...")
    else:
        print("   ℹ️ 購物車明細已顯示於右側欄或無需額外切換按鈕，直接檢視目前購物車...")

    # 嘗試擷取購物車 / 結帳區塊文字與截圖
    cart_container = find_first_visible(
        page,
        [
            '[data-testid*="cart"]',
            'aside',
            '[class*="cart"]',
            'main',
        ],
        timeout_ms=3000,
    )
    raw_text = ""
    if cart_container is not None:
        try:
            raw_text = cart_container.inner_text(timeout=3000)
        except Exception:
            raw_text = ""

    if not raw_text:
        try:
            raw_text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            raw_text = ""

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

    # 先以規則解析總計金額作為備援（抓取「總計」或「Total」附近的金額數字）
    total_amount = "請查看瀏覽器畫面確認"
    for i, line in enumerate(lines):
        if any(key in line for key in ("總計", "總金額", "Total", "應付")):
            window = " ".join(lines[i : i + 3])
            match = re.search(r"(?:NT\$|\$)\s*[\d,]+(?:\.\d+)?", window)
            if match:
                total_amount = match.group(0)
                break

    item_lines: list[str] = []
    if ai_client is not None:
        try:
            cart_png: bytes | None = None
            if cart_container is not None:
                try:
                    box = cart_container.bounding_box()
                    if box and box["width"] >= 200 and box["height"] >= 200:
                        cart_png = cart_container.screenshot(type="png")
                except Exception:
                    cart_png = None
            if cart_png is None:
                cart_png = page.screenshot(type="png")

            print("   🤖 正在使用 AI 檢視目前購物車實際品項...")
            ai_items, ai_total = ai_inspect_cart(ai_client, ai_model, cart_png, raw_text)
            item_lines = ai_items
            if ai_total:
                total_amount = ai_total
        except AIAbortError:
            raise
        except Exception as exc:
            print(f"   ⚠️ AI 檢視購物車失敗（{exc}），改用文字擷取...")

    if not item_lines:
        skip_keywords = (
            "查看購物車",
            "查看付款方式",
            "前往結帳",
            "小計",
            "外送費",
            "服務費",
            "平台費",
            "總計",
            "查看明細",
            "外送",
            "外帶自取",
            "尚未提供服務",
            "加購其他商品",
            "購物車目前空空的",
        )
        for line in lines:
            if any(skip in line for skip in skip_keywords):
                continue
            if len(line) <= 60:
                item_lines.append(line)

    print("🛑 已完成購物車檢視，立即停止瀏覽器自動操作（不會自動送出付款）。")
    return item_lines[:30], total_amount


def format_notification_message(
    extracted_lines: list[str],
    summary_items: list[dict[str, Any]],
    total_amount: str,
    shop_name: str = "",
) -> str:
    """組合通知訊息，直接呈現目前購物車內實際檢視到的品項與總計金額。"""
    if extracted_lines:
        items_block = "\n".join(f"  • {line}" for line in extracted_lines)
    else:
        items_block = "  • （目前購物車為空，或無法自動辨識品項，請查看瀏覽器畫面確認）"

    shop_line = f"🏪 店家：{shop_name}\n" if shop_name else ""
    return (
        "🛒 新訂單已準備好！\n"
        f"{shop_line}"
        f"📦 目前購物車品項：\n{items_block}\n"
        f"💰 總計金額：{total_amount}\n"
        "請手動完成付款。"
    )


def user_notification(message: str, group_id: str | None = None, send_line: bool = False) -> None:
    """輸出通知訊息；若指定 --notify-line 則透過 LINE Messaging API 推播。"""
    print("\n================ 通知內容 ================")
    print(message)
    print("==========================================")

    if not send_line:
        return

    load_dotenv(DEFAULT_ENV_PATH)
    access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    if not access_token or not group_id:
        print("⚠️ 未設定 LINE_CHANNEL_ACCESS_TOKEN 或訂單缺少 group_id，略過 LINE 推播。")
        return

    from linebot.v3.messaging import ApiClient, Configuration, MessagingApi, PushMessageRequest, TextMessage

    config = Configuration(access_token=access_token)
    with ApiClient(config) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=group_id,
                messages=[TextMessage(text=message)],
            )
        )
    print(f"📨 已發送 LINE 通知至：{group_id}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本機 Playwright foodpanda 自動點餐準備腳本")
    parser.add_argument("--order-file", type=str, default=None, help="指定訂單 JSON 路徑（預設取最新一筆）")
    parser.add_argument("--orders-dir", type=str, default=str(DEFAULT_ORDERS_DIR), help="訂單資料夾路徑")
    parser.add_argument("--profile-dir", type=str, default=str(DEFAULT_PROFILE_DIR), help="Chrome 登入狀態保存資料夾")
    parser.add_argument(
        "--browser",
        choices=("chrome", "chromium"),
        default="chrome",
        help="優先使用的瀏覽器（預設 chrome，若未安裝自動退回 chromium）",
    )
    parser.add_argument(
        "--cdp-url",
        type=str,
        default=None,
        help="選填：連接已開啟的 Chrome 遠端除錯位址（例如 http://127.0.0.1:9222）",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="僅開啟瀏覽器讓你登入 foodpanda 與設定外送地址，完成後按 Enter 保存狀態",
    )
    parser.add_argument("--notify-line", action="store_true", help="是否同時透過 LINE Bot 推播通知")
    parser.add_argument("--line-to", type=str, default=None, help="選填：指定 LINE 推播對象 ID（覆寫訂單內的 group_id）")
    parser.add_argument("--no-ai", action="store_true", help="停用 OpenAI 思考點擊，僅使用純規則模式")
    parser.add_argument("--ai-model", type=str, default=DEFAULT_AI_MODEL, help="指定 OpenAI 模型 ID（預設 gpt-6-luna）")
    parser.add_argument("--openai-api-key", type=str, default=None, help="選填：直接傳入 OPENAI_API_KEY")
    parser.add_argument("--openai-base-url", type=str, default=None, help="選填：OpenAI 相容 API Base URL")
    parser.add_argument(
        "--close-on-exit",
        action="store_true",
        help="程式結束後關閉瀏覽器（預設保持瀏覽器開啟，方便直接查看或繼續操作）",
    )
    return parser.parse_args()


def find_chrome_executable(playwright: Any, prefer_chrome: bool = True) -> str | None:
    """尋找本機 Google Chrome 執行檔，找不到時退回 Playwright 內建 Chromium。"""
    if prefer_chrome:
        candidates = [
            shutil.which("chrome"),
            shutil.which("google-chrome"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
        ]
        for path in candidates:
            if path and Path(path).is_file():
                return str(path)
    try:
        exe = playwright.chromium.executable_path
        if exe and Path(exe).is_file():
            return str(exe)
    except Exception:
        pass
    return None


def is_cdp_ready(port: int) -> bool:
    """檢查本機 CDP 除錯埠是否已就緒。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.6) as resp:
            return resp.status == 200
    except Exception:
        return False


def find_free_port() -> int:
    """取得一個可用的本機 TCP 埠號。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def launch_detached_browser_cdp(
    playwright: Any,
    profile_dir: Path,
    prefer_chrome: bool = True,
) -> str | None:
    """以獨立行程啟動瀏覽器並開啟 CDP，使 Python 結束或中斷後瀏覽器仍保持開啟。"""
    profile_dir.mkdir(parents=True, exist_ok=True)
    port_file = profile_dir / ".cdp_port"

    if port_file.is_file():
        try:
            saved_port = int(port_file.read_text(encoding="utf-8").strip())
            if is_cdp_ready(saved_port):
                print(f"🔗 偵測到先前保持開啟的瀏覽器（port {saved_port}），直接沿用視窗...")
                return f"http://127.0.0.1:{saved_port}"
        except Exception:
            pass

    exe = find_chrome_executable(playwright, prefer_chrome=prefer_chrome)
    if not exe:
        return None

    port = find_free_port()
    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--window-size=1280,900",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(cmd, **popen_kwargs)
    except Exception as exc:
        print(f"⚠️ 無法以獨立模式啟動瀏覽器（{exc}），改用 Playwright 啟動...")
        return None

    deadline = time.time() + 8.0
    while time.time() < deadline:
        if is_cdp_ready(port):
            try:
                port_file.write_text(str(port), encoding="utf-8")
            except Exception:
                pass
            return f"http://127.0.0.1:{port}"
        time.sleep(0.2)

    return None


def open_browser_context(playwright: Any, args: argparse.Namespace) -> tuple[Any, Page, bool]:
    """啟動或連接非無頭瀏覽器，回傳 (browser_or_context, page, should_close)。"""
    cdp_url = args.cdp_url
    if not cdp_url and not args.close_on_exit:
        cdp_url = launch_detached_browser_cdp(
            playwright,
            Path(args.profile_dir),
            prefer_chrome=(args.browser == "chrome"),
        )

    if cdp_url:
        print(f"🔗 連接獨立瀏覽器 CDP（結束後將保持視窗開啟）：{cdp_url}")
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        return browser, page, False

    profile_dir = Path(args.profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }

    if args.browser == "chrome":
        try:
            context = playwright.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            return context, page, True
        except Exception as exc:
            print(f"⚠️ 無法啟動本機 Google Chrome（{exc}），改用 Playwright Chromium。")

    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    page = context.pages[0] if context.pages else context.new_page()
    return context, page, True


def main() -> None:
    args = parse_args()

    if not args.login:
        order_data = load_order_data(Path(args.orders_dir), args.order_file)
        metadata = order_data.get("metadata", {})
        summary_items = order_data.get("summary_items", [])
        shop_name = metadata["shop_name"]
        group_id = metadata.get("group_id")

    with sync_playwright() as p:
        handle, page, should_close = open_browser_context(p, args)

        try:
            if args.login:
                print(f"\n🔐 登入設定模式：前往 {FOODPANDA_URL}")
                page.goto(FOODPANDA_URL, wait_until="domcontentloaded", timeout=60000)
                print(f"📁 登入狀態將保存於：{args.profile_dir}")
                input(
                    "\n👉 請在開啟的瀏覽器完成：\n"
                    "   1. 通過機器人驗證（若有）\n"
                    "   2. 登入你的 foodpanda 帳號\n"
                    "   3. 設定好常用外送地址\n"
                    "完成後回到終端機按 Enter 保存並繼續..."
                )
                return

            ai_client = get_openai_client(
                use_ai=not args.no_ai,
                api_key_override=args.openai_api_key,
                base_url_override=args.openai_base_url,
            )
            if ai_client is not None:
                print(f"🤖 OpenAI 思考點擊模式：已啟用（模型：{args.ai_model}）")
            else:
                print("⚙️ 目前使用純規則點擊模式")

            search_and_enter_shop(page, shop_name)
            add_menu_items(page, summary_items, ai_client=ai_client, ai_model=args.ai_model)
            extracted_lines, total_amount = view_cart_and_extract(
                page,
                ai_client=ai_client,
                ai_model=args.ai_model,
            )

            msg = format_notification_message(extracted_lines, summary_items, total_amount, shop_name=shop_name)
            user_notification(msg, group_id=(args.line_to or group_id), send_line=args.notify_line)

            if should_close:
                input("\n💳 請在瀏覽器手動確認品項並完成付款。完成後按 Enter 關閉瀏覽器...")
            else:
                print("\n🌐 瀏覽器將保持開啟狀態，請直接在視窗中確認購物車品項並完成付款。")
        except AIAbortError as exc:
            print(f"\n🛑 {exc}")
            if not should_close:
                print("🌐 瀏覽器仍保持開啟，方便你接手確認或繼續操作。")
            sys.exit(1)
        finally:
            print_token_summary(args.ai_model)
            if should_close and args.close_on_exit:
                handle.close()
            elif should_close:
                try:
                    handle.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
