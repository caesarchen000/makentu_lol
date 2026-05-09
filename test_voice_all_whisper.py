import json
import re
import sys
import os
import time
import subprocess
from pathlib import Path
import sounddevice as sd
import numpy as np
import keyboard
import whisper # 🌟 引入 Whisper
from openai import OpenAI

from openai_key_util import load_openai_api_key

# ==========================================
# ⚙️ 1. 設定 OpenAI API（OPENAI_API_KEY 環境變數，或專案根目錄 .env）
# ==========================================
BASE_DIR = Path(__file__).resolve().parent
API_KEY = load_openai_api_key(base_dir=BASE_DIR).strip()
MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
if not API_KEY:
    print("\n[錯誤] 請設定 OPENAI_API_KEY（環境變數）或在專案根目錄建立 .env")
    sys.exit(1)
openai_client = OpenAI(api_key=API_KEY)
PIPELINE_JSON_PATH = BASE_DIR / "pipeline_payload.json"
CLASSIFIER_SCRIPT_PATH = BASE_DIR / "message_classifier.py"
OPENAI_TIMEOUT_SECONDS = 12
CLASSIFIER_TIMEOUT_SECONDS = 8
IMAGES_ROOT = BASE_DIR / "DemoPlugin" / "DemoPlugin" / "images"
CHARACTERS_DIR = IMAGES_ROOT / "characters"
SKILLS_DIR = IMAGES_ROOT / "skills"


def _list_png_stems(folder: Path):
    if not folder.exists():
        return []
    stems = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() == ".png":
            stems.append(p.stem.strip())
    return sorted(set(s for s in stems if s))


CHARACTER_NAME_LIST = _list_png_stems(CHARACTERS_DIR)
SKILL_NAME_LIST = _list_png_stems(SKILLS_DIR)
CHARACTER_NAMES_PROMPT = "、".join(CHARACTER_NAME_LIST) if CHARACTER_NAME_LIST else "（未偵測到角色圖檔）"
SKILL_NAMES_PROMPT = "、".join(SKILL_NAME_LIST) if SKILL_NAME_LIST else "（未偵測到技能圖檔）"

def rewrite_with_llm(raw_text):
    prompt = f"""
    你現在是一個台灣《英雄聯盟》(LOL) 的高端玩家。你的任務是把語音辨識出來的句子，精簡並轉換成「台服 LOL 遊戲內對話框會出現的極簡術語」。

    【核心規則】
    1. 極度簡短：能用 2 個字表達，就不要用 3 個字，節省打字與閱讀時間。
    2. 絕對安靜：不准有任何解釋、問候語、引號或標點符號，只輸出最終的字。
    3. 自動糾錯：語音辨識常有錯字（例如"江山"或"较少"=交閃，"大爷"=打野，"没伞"=沒閃，"小时"=消失，"车队"=撤退），請根據 LOL 情境自動修正。

    【台服專屬術語字典】
    - 位置：上路(top), 中路(mid), 下路(bot/下), 打野(jg), 輔助(sup), 射手(ad)
    - 技能與狀態：交閃現(沒閃/交閃), 沒大絕(沒大/no r), 傳送(tp), 治癒(he), 沒魔(沒藍/oom), 殘血(一滴/大殘)
    - 戰術動作：吃龍(開龍), 撤退(拉掉/退), 推進(推線), 蹲點(在蹲), 消失(miss/不見), 回城(b)
    - 戰鬥指揮：打到底(一波), 殺後排(抓ad/抓sup), 保護(保排), 開戰(開他/能打)

    【翻譯範例】
    語音：「對面中路剛剛把閃現交掉了」 -> 輸出：中路沒閃
    語音：「大爷在下路草叢蹲很久了」 -> 輸出：打野在下蹲
    語音：「我們把這條小龍放掉吧打不贏」 -> 輸出：放龍
    語音：「我沒有魔力了我要先回家」 -> 輸出：沒魔 回城
    語音：「先殺對面的射手可以一波」 -> 輸出：先抓射手 一波
    語音：「上路江山了」 -> 輸出：上路沒閃
    
    使用者語音：「{raw_text}」
    轉換後的LOL術語：
    """
    try:
        response = openai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=15,
            temperature=0.1,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"\n[LLM 錯誤] 無法連線至 AI，錯誤訊息: {e}")
        return raw_text


def _extract_json_object(text):
    """從模型回覆中取出第一個 JSON 物件字串（允許外層 markdown 程式碼區塊）。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def _extract_json_blob(text):
    """Extract first JSON blob (object or array) from model output."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    # Prefer array if present and wraps objects.
    arr_start = text.find("[")
    arr_end = text.rfind("]")
    obj_start = text.find("{")
    obj_end = text.rfind("}")

    if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
        return text[arr_start : arr_end + 1]
    if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
        return text[obj_start : obj_end + 1]
    return None


def _normalize_payloads(data):
    """
    Accept either one object or a list of objects.
    Returns non-empty list of validated payload dicts.
    """
    items = data if isinstance(data, list) else [data]
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        target = str(item.get("target", "")).strip()
        slang = str(item.get("lol_slang_line", "")).strip()
        if kind not in ("chat", "status_report"):
            continue
        if not slang:
            continue
        normalized.append(
            {
                "kind": kind,
                "target": target,
                "lol_slang_line": slang,
            }
        )
    return normalized


def _keep_chinese_text(text: str) -> str:
    """Keep only Chinese chars and common Chinese punctuation/spaces."""
    if not text:
        return ""
    filtered = re.sub(r"[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\s]", "", text)
    filtered = re.sub(r"\s+", "", filtered).strip()
    return filtered


def _normalize_to_chinese_text(text: str) -> str:
    """
    Keep existing Chinese as-is, and translate only non-Chinese parts into Chinese.
    Always run this step regardless of Chinese ratio.
    """
    src = (text or "").strip()
    if not src:
        return ""

    prompt = f"""
你是繁體中文電競語音校正助手。請把句子中的「非中文片段」翻譯成繁體中文，
但原本已經是中文的內容請盡量保留原意，不要亂改。
可修正常見語音辨識錯字。
只輸出最終句子，不要解釋。

輸入：
{src}
"""
    try:
        response = openai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.1,
            timeout=OPENAI_TIMEOUT_SECONDS,
        )
        translated = (response.choices[0].message.content or "").strip()
        return _keep_chinese_text(translated)
    except Exception:
        # If translation fails, keep best-effort Chinese extraction.
        return _keep_chinese_text(src)


def analyze_voice_to_structured_json(raw_text):
    """
    將語音轉成固定結構的 JSON（dict 或 list[dict]）：
    - 訊息種類、對象、台服極簡術語行
    只輸出 JSON，無其他文字。
    """
    prompt = f"""
你是台灣《英雄聯盟》(LOL) 高端玩家與通訊分類器。
請根據「使用者語音轉寫」判斷是單一事件還是多個事件：
- 單一事件：輸出一個 JSON 物件
- 多個事件：輸出 JSON 陣列，每個元素一個事件

【輸出規則】
1. 只輸出 JSON，不要 markdown、不要說明、不要前後文字。
2. 每個事件必須包含鍵：kind, target, lol_slang_line。
3. 欄位：
   - kind：chat | status_report
   - target：這句話的主要目標（英雄/玩家/路線/物件），例如「阿璃」；若無明確目標請填空字串
   - lol_slang_line：台服極簡術語一行（極短、無多餘標點，符合遊戲內打字習慣）
4. 術語與糾錯沿用台服習慣（江山/较少→交閃語境、大爷→打野、没伞→沒閃、小时→消失、车队→撤退等）。
5. 範例1：
   使用者語音轉寫：「阿璃沒有瞬移」
   請輸出：
   {{
       "kind": "status_report",
       "target": "阿璃",
       "lol_slang_line": "阿璃沒閃"
   }}
   範例2:
   使用者語音轉寫：「阿卡麗在上路草叢」
   請輸出：
   {{
       "kind": "chat",
       "target": "阿卡麗",
       "lol_slang_line": "阿卡麗在上草"
   }}

6. 可用英雄名稱（優先使用以下中文名稱，避免拼音/英文）：
   {CHARACTER_NAMES_PROMPT}
7. 可用技能名稱（優先使用以下名稱做糾錯與歸一化）：
   {SKILL_NAMES_PROMPT}

使用者語音轉寫：
「{raw_text}」
"""
    try:
        t0 = time.time()
        print("  [Pipeline] 開始呼叫 OpenAI 產生 JSON...")
        response = openai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0.2,
            timeout=OPENAI_TIMEOUT_SECONDS,
        )
        print(f"  [Pipeline] OpenAI 完成，耗時 {time.time() - t0:.2f}s")
        raw = (response.choices[0].message.content or "").strip()
        blob = _extract_json_blob(raw)
        if not blob:
            raise ValueError("無法從模型回覆中擷取 JSON")
        data = json.loads(blob)
        payloads = _normalize_payloads(data)
        if not payloads:
            raise ValueError("JSON 內容沒有有效事件")
        return payloads
    except Exception as e:
        print(f"\n[結構化 JSON 失敗，改用純文字後備] {e}")
        slang = rewrite_with_llm(raw_text)
        return [
            {
                "kind": "chat",
                "target": "",
                "lol_slang_line": slang,
            }
        ]


def run_message_pipeline(payloads):
    """Save each payload and run message_classifier.py one by one."""
    for idx, payload in enumerate(payloads, start=1):
        PIPELINE_JSON_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  [Pipeline] JSON({idx}/{len(payloads)}) 已寫入：{PIPELINE_JSON_PATH}")

        print(f"  [Pipeline] 執行 message_classifier.py ({idx}/{len(payloads)})...")
        t0 = time.time()
        result = subprocess.run(
            [sys.executable, str(CLASSIFIER_SCRIPT_PATH), str(PIPELINE_JSON_PATH)],
            capture_output=True,
            text=True,
            timeout=CLASSIFIER_TIMEOUT_SECONDS,
        )
        print(f"  [Pipeline] classifier 完成，耗時 {time.time() - t0:.2f}s")

        if result.stdout.strip():
            print(result.stdout.strip())
        if result.returncode != 0:
            if result.stderr.strip():
                print(result.stderr.strip())
            raise RuntimeError(f"message_classifier.py failed with exit code {result.returncode}")

# ==========================================
# ⚙️ 2. 初始化 Whisper 語音模型
# ==========================================
print("正在載入 Whisper 語音模型 (可能需要幾秒鐘)...")
# 🌟 這裡使用 'small' 模型，準確度極高，且一般電腦跑得動
# 如果你覺得太慢，可以換成 'base' 或 'tiny'
whisper_model = whisper.load_model("small") 

# ==========================================
# ⚙️ 3. 核心設定字典 (保留燈號專用)
# ==========================================
# 因為 Whisper 很聰明，其實不太會聽錯，但為了燈號的瞬間觸發，我們還是保留
CORRECTIONS = {
    "小时": "消失", "不见": "不見", "危险": "危險", "撤退": "撤退", 
    "路上": "路上", "来了": "來了", "帮忙": "幫忙", "救命": "救命"
}

HOTKEYS = {
    "消失": "f5", "不見": "f5", "危險": "f6", "撤退": "f6",
    "路上": "f7", "來了": "f7", "幫忙": "f8", "救命": "f8"
}

# ==========================================
# 🚀 4. 主程式邏輯 (按鍵錄音機制)
# ==========================================
print("\n模型載入完畢！")
print("=======================================")
print("🎙️ Whisper 語音助理已啟動！")
print("👉 請【按住 Shift 鍵】說話，【放開 Shift 鍵】就會自動辨識並發送。")
print("按 Ctrl+C 可強制結束程式")
print("=======================================")

try:
    while True:
        # 等待使用者按下 Shift 鍵
        if keyboard.is_pressed('shift'):
            print("\n🔴 錄音中... (請講話，講完放開 Shift)")
            
            # 開始錄音的容器
            audio_data = []
            
            def callback(indata, frames, time, status):
                """將麥克風收到的音訊片段塞進容器"""
                audio_data.append(indata.copy())

            # 開啟麥克風錄音 (Whisper 預設吃 16000Hz, float32 的格式)
            with sd.InputStream(samplerate=16000, channels=1, dtype='float32', callback=callback):
                # 只要 Shift 還按著，就一直維持錄音狀態
                while keyboard.is_pressed('shift'):
                    time.sleep(0.05)
            
            # 當程式走到這裡，代表使用者放開了 Shift 鍵
            print("🟢 錄音結束，Whisper 辨識中...")
            
            # 將錄下來的聲音碎片組裝成一個完整的陣列
            if len(audio_data) > 0:
                audio_np = np.concatenate(audio_data, axis=0).flatten()
                
                # 確保錄音不是太短 (大於 0.5 秒才處理，過濾誤觸)
                if len(audio_np) > 16000 * 0.5:
                    # 呼叫 Whisper 進行辨識
                    # fp16=False 是為了解決某些沒有高階 GPU 的電腦會報錯的問題
                    result = whisper_model.transcribe(audio_np, language="zh", fp16=False)
                    raw_text = (result.get("text") or "").strip()
                    text = _normalize_to_chinese_text(raw_text)
                    
                    if text:
                        print(f"[Whisper 聽到]：{text}")
                        
                        # 步驟一：針對「燈號」進行本地端快速校正
                        hotkey_text = text
                        for wrong_word, correct_word in CORRECTIONS.items():
                            if wrong_word in hotkey_text:
                                hotkey_text = hotkey_text.replace(wrong_word, correct_word)
                        
                        # 步驟二：檢查是否為燈號指令 (瞬間觸發燈號)
                        for keyword, key in HOTKEYS.items():
                            if keyword in hotkey_text:
                                print(f"  👉 [模式 A] 觸發燈號！瞬間按下：【 {key.upper()} 】鍵")
                                keyboard.send(key)
                                # 💡 這裡移除了 is_hotkey_triggered，讓程式不要停下來
                                break 
                        
                        # 步驟三：無論有沒有發燈號，都繼續呼叫 AI 產生結構化 JSON + 術語行
                        print("  [模式 B] 呼叫 AI 產生結構化 JSON...")
                        payloads = analyze_voice_to_structured_json(text)
                        print("  [結構化 JSON]：")
                        print(json.dumps(payloads, ensure_ascii=False, indent=2))
                        run_message_pipeline(payloads)
                    else:
                        print("⚠️ Whisper 未辨識到中文內容，已忽略。")
                else:
                    print("⚠️ 錄音太短，已忽略。")
            else:
                print("⚠️ 沒有收到麥克風音訊（可能是權限或輸入裝置問題）。")
            
            # 辨識完畢後，稍微等一下避免連點
            while keyboard.is_pressed('shift'):
                time.sleep(0.05)
            time.sleep(0.2)
        else:
            time.sleep(0.05)
            
except KeyboardInterrupt:
    print("\n\n程式已結束。")
except Exception as e:
    print(f"\n\n發生錯誤: {e}")