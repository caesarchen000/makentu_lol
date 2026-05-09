"""
tactical_client_cloud.py — Central tactical voice client.

5v5 LOL voice system:
  - Mic is ALWAYS ON — no PTT button needed to start talking
  - Audio is tagged ALL by default; PTT_ALLY*_TOGGLE buttons change who receives it
  - VAD (Voice Activity Detection) segments speech automatically — no manual start/stop
  - Whisper + OpenAI run on every detected speech segment
  - CMD:ENEMY_ALERT is always broadcast to ALL via RPi
  - START<n>F/T signals trigger countdown on the local Loupedeck console

Usage:
    python tactical_client_cloud.py
"""

import json
import os
import queue
import re
import socket
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import whisper
from openai import OpenAI

from openai_key_util import load_openai_api_key

# =====================================================================
# ⚙️  Configuration
# =====================================================================
BASE_DIR    = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "tactical_config.json"


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "server_ip":   "172.20.10.2",
        "server_port": 5005,
        "my_role":     "MID",
        "my_hero":     "蓋倫",
        "allies":      ["JG", "TOP", "BOT", "SUP"],
        "enemies":     ["安妮", "好運姐", "阿姆姆", "雷歐娜", "墨菲特"],
    }


config = load_config()

RPI_IP            = config.get("server_ip",   "172.20.10.2")
UDP_PORT          = config.get("server_port",  5005)
LOCAL_IPC_PORT    = 5006   # Loupedeck plugin → this client
LOCAL_PLUGIN_PORT = 5005   # this client → Loupedeck plugin

MY_ROLE  = config.get("my_role",  "MID")
MY_HERO  = config.get("my_hero",  "")
ALLIES   = config.get("allies",   [])[:4]
ENEMIES  = config.get("enemies",  [])[:5]

CHANNELS = 1
RATE     = 16000
CHUNK    = 1024   # ~64 ms per callback block

# =====================================================================
# ⚙️  VAD parameters (tunable)
# =====================================================================
VAD_SPEECH_RMS    = 0.01   # float32 RMS threshold — above this = speaking
VAD_SILENCE_TIMEOUT = 1.5  # seconds of silence after speech → flush to Whisper
VAD_MIN_DURATION  = 0.5    # seconds — discard shorter segments (noise bursts)
VAD_MAX_DURATION  = 10.0   # seconds — force-flush even if still speaking

# =====================================================================
# ⚙️  OpenAI client — initialised once at startup
# =====================================================================
API_KEY    = load_openai_api_key(base_dir=BASE_DIR).strip()
MODEL_NAME             = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT_SECONDS = 12

if not API_KEY:
    print("[警告] 未設定 OPENAI_API_KEY（可用環境變數或專案根目錄 .env），AI 分析功能將無法使用。")
    openai_client = None
else:
    openai_client = OpenAI(api_key=API_KEY)
    print(f"✅ OpenAI client 已初始化 (model={MODEL_NAME})")

# =====================================================================
# ⚙️  LOL corrections & hotkeys  (mirrored from test_voice_all_whisper.py)
# =====================================================================
CORRECTIONS = {
    "小时": "消失", "不见": "不見", "危险": "危險", "撤退": "撤退",
    "路上": "路上", "来了": "來了", "帮忙": "幫忙", "救命": "救命",
}
HOTKEYS = {
    "消失": "f5", "不見": "f5", "危險": "f6", "撤退": "f6",
    "路上": "f7", "來了": "f7", "幫忙": "f8", "救命": "f8",
}

# =====================================================================
# ⚙️  Global state
# =====================================================================
is_running = True
state_lock = threading.Lock()

# Routing tag — 4-byte UDP header.  ALL by default; changed by ally toggles.
# Written only from ipc_listener thread; read from mic callback (lock-free OK
# because Python bytes assignment is atomic on CPython).
current_tag: bytes = b"ALL "

# Ally channel toggle state
enabled_ally_slots        = set(range(1, len(ALLIES) + 1))
pending_ally_slots        = set(enabled_ally_slots)
selection_window_deadline = 0.0

# Queue: mic callback → VAD thread
vad_queue: queue.Queue = queue.Queue(maxsize=2048)

# =====================================================================
# ⚙️  Audio output (plays incoming ally audio from RPi)
# =====================================================================
stream_out = sd.RawOutputStream(
    samplerate=RATE, channels=CHANNELS, dtype="int16", blocksize=CHUNK
)
stream_out.start()

# =====================================================================
# ⚙️  Network
# =====================================================================
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", 0))
sock.sendto(f"HELLO:{MY_ROLE}".encode(), (RPI_IP, UDP_PORT))
print(f"已向伺服器註冊身分: [{MY_ROLE}] → {RPI_IP}:{UDP_PORT}")

# =====================================================================
# 🔧  Send config to Loupedeck plugin
# =====================================================================
def send_config_to_plugin():
    psock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        psock.sendto(f"CONFIG:MY_ROLE:{MY_ROLE}".encode(), ("127.0.0.1", LOCAL_PLUGIN_PORT))
        for i, role in enumerate(ALLIES, 1):
            psock.sendto(f"CONFIG:ALLY{i}:{role}".encode(), ("127.0.0.1", LOCAL_PLUGIN_PORT))
        for i, hero in enumerate(ENEMIES, 1):
            psock.sendto(f"CONFIG:ENEMY{i}:{hero}".encode(), ("127.0.0.1", LOCAL_PLUGIN_PORT))
        print(f"📤 Config 已發送至 Loupedeck Plugin (port {LOCAL_PLUGIN_PORT})")
    finally:
        psock.close()

# =====================================================================
# 🧹  Process cleanup — PID file (reliable on Windows)
# =====================================================================
PID_FILE = BASE_DIR / ".tactical_client.pid"


def _kill_previous_instance():
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
        if old_pid == os.getpid():
            return
        import subprocess as sp
        check = sp.run(
            ["tasklist", "/FI", f"PID eq {old_pid}"],
            capture_output=True, text=True, timeout=5,
        )
        if str(old_pid) in check.stdout:
            sp.run(["taskkill", "/F", "/PID", str(old_pid)],
                   capture_output=True, timeout=5)
            print(f"🧹 已終止上次殘留程序 (PID {old_pid})")
            time.sleep(0.5)
    except Exception:
        pass
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def _write_pid():
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass


_kill_previous_instance()
_write_pid()

# =====================================================================
# 🎙️  Always-on mic callback
#
#  Two jobs per chunk:
#    1. UDP relay  → RPi with current_tag (ALL or specific role)
#    2. VAD feed   → push into vad_queue for the VAD thread
# =====================================================================
def _mic_callback(indata, frames, callback_time, status):
    """sounddevice calls this for every CHUNK frames, always."""
    if status:
        print(f"⚠️ 麥克風: {status}")

    mono = indata[:, 0]   # shape (CHUNK,), float32

    # 1. UDP relay — convert to int16 PCM and tag with routing header
    pcm16 = (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    tag   = current_tag   # read atomic bytes reference
    try:
        sock.sendto(tag + pcm16, (RPI_IP, UDP_PORT))
    except Exception:
        pass

    # 2. VAD feed — push float32 chunk (copy so callback returns fast)
    try:
        vad_queue.put_nowait(mono.copy())
    except queue.Full:
        pass   # drop oldest-style: VAD thread is behind, skip this chunk


# =====================================================================
# 🔊  VAD thread — segments speech and dispatches to Whisper pipeline
# =====================================================================
def vad_loop():
    """
    Energy-based Voice Activity Detection.

    State machine:
      SILENCE  — waiting for speech onset
      SPEECH   — accumulating speech frames
      TRAILING — speech ended, counting silence before flush
    """
    speech_buf   = []
    in_speech    = False
    silence_start = None

    while is_running:
        try:
            chunk = vad_queue.get(timeout=0.5)
        except queue.Empty:
            if in_speech and speech_buf:
                dur = len(speech_buf) * CHUNK / RATE
                # Force-flush on max duration
                if dur >= VAD_MAX_DURATION:
                    _flush_speech(speech_buf)
                    speech_buf    = []
                    in_speech     = False
                    silence_start = None
                # Also flush if silence timeout has elapsed since last sound
                elif silence_start is not None and (time.time() - silence_start) >= VAD_SILENCE_TIMEOUT:
                    _flush_speech(speech_buf)
                    speech_buf    = []
                    in_speech     = False
                    silence_start = None
            continue

        rms = float(np.sqrt(np.mean(chunk ** 2)))

        if rms > VAD_SPEECH_RMS:
            # Active speech
            in_speech     = True
            silence_start = None
            speech_buf.append(chunk)

            # Force-flush if duration exceeded
            dur = len(speech_buf) * CHUNK / RATE
            if dur >= VAD_MAX_DURATION:
                _flush_speech(speech_buf)
                speech_buf    = []
                in_speech     = False
                silence_start = None

        elif in_speech:
            # Trailing silence after speech
            speech_buf.append(chunk)
            if silence_start is None:
                silence_start = time.time()
            elif time.time() - silence_start >= VAD_SILENCE_TIMEOUT:
                _flush_speech(speech_buf)
                speech_buf    = []
                in_speech     = False
                silence_start = None
        # else: pure silence before any speech — discard chunk


def _flush_speech(buf: list):
    """Concatenate collected chunks and dispatch to voice_analysis_pipeline."""
    audio_np = np.concatenate(buf).flatten()
    duration = len(audio_np) / RATE
    if duration < VAD_MIN_DURATION:
        return
    print(f"[VAD] 偵測到語音 {duration:.1f}s → Whisper")
    threading.Thread(
        target=voice_analysis_pipeline,
        args=(audio_np,),
        daemon=True,
    ).start()


# =====================================================================
# 🎙️  IPC listener — receives ally toggle commands from Loupedeck plugin
# =====================================================================
def ipc_listener():
    global current_tag, pending_ally_slots, selection_window_deadline, enabled_ally_slots

    ipc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for attempt in range(5):
        try:
            ipc_sock.bind(("127.0.0.1", LOCAL_IPC_PORT))
            break
        except OSError as e:
            if attempt < 4:
                print(f"⏳ Port {LOCAL_IPC_PORT} 尚未釋放，重試中... ({attempt+1}/5)")
                time.sleep(1)
            else:
                print(f"❌ 無法綁定 port {LOCAL_IPC_PORT}: {e}")
                return
    ipc_sock.settimeout(1.0)
    print(f"🔌 IPC 伺服器已啟動 (Port: {LOCAL_IPC_PORT})，等待 Logi Console 指令...")

    def _commit_pending_if_due(now_ts):
        global pending_ally_slots, selection_window_deadline, enabled_ally_slots, current_tag
        if selection_window_deadline <= 0 or now_ts < selection_window_deadline:
            return
        with state_lock:
            committed = set(pending_ally_slots)
            if not committed:
                committed = set(range(1, len(ALLIES) + 1))
            enabled_ally_slots    = committed
            selection_window_deadline = 0.0
            role_names = [ALLIES[i - 1] for i in sorted(enabled_ally_slots) if 1 <= i <= len(ALLIES)]

        # Update routing tag
        if len(enabled_ally_slots) >= len(ALLIES):
            current_tag = b"ALL "
            print(f"🧭 路由 → ALL")
        else:
            first_slot = min(enabled_ally_slots)
            role = ALLIES[first_slot - 1] if 1 <= first_slot <= len(ALLIES) else "ALL"
            current_tag = role.ljust(4)[:4].encode()
            print(f"🧭 路由 → {role_names}")

    while is_running:
        try:
            _commit_pending_if_due(time.time())
            data, _ = ipc_sock.recvfrom(1024)
            msg = data.decode("utf-8").strip()

            if msg.startswith("PTT_ALLY") and msg.endswith("_TOGGLE"):
                try:
                    slot = int(msg.replace("PTT_ALLY", "").replace("_TOGGLE", ""))
                    if 1 <= slot <= len(ALLIES):
                        with state_lock:
                            if selection_window_deadline <= 0:
                                all_green = len(enabled_ally_slots) == len(ALLIES)
                                pending_ally_slots = set() if all_green else set(enabled_ally_slots)
                            if slot in pending_ally_slots:
                                pending_ally_slots.remove(slot)
                            else:
                                pending_ally_slots.add(slot)
                            selection_window_deadline = time.time() + 0.5
                except ValueError:
                    pass

            elif msg == "PTT_ALL_TOGGLE":
                with state_lock:
                    enabled_ally_slots = set(range(1, len(ALLIES) + 1))
                current_tag = b"ALL "
                print("🧭 路由 → ALL")

        except socket.timeout:
            _commit_pending_if_due(time.time())
        except Exception as e:
            if is_running:
                print(f"IPC 錯誤: {e}")

    ipc_sock.close()


# =====================================================================
# 📡  Audio receiver — plays incoming ally voice from RPi
# =====================================================================
def receive_and_play():
    print("📡 監聽戰術頻道中...")
    while is_running:
        try:
            data, _ = sock.recvfrom(8192)
            if len(data) <= 4:
                continue
            try:
                preview = data[:4].decode("utf-8")
            except UnicodeDecodeError:
                preview = ""

            if preview == "CMD:":
                handle_incoming_command(data)
                continue

            # Normal audio: first 4 bytes = sender role tag, rest = int16 PCM
            stream_out.write(data[4:])
        except Exception:
            pass


def handle_incoming_command(data: bytes):
    try:
        cmd_text = data.decode("utf-8")

        if cmd_text.startswith("CMD:ENEMY_ALERT:"):
            json_str = cmd_text[len("CMD:ENEMY_ALERT:"):]
            try:
                alert = json.loads(json_str)
            except json.JSONDecodeError as e:
                print(f"⚠️ JSON 解析失敗: {e}")
                return
            hero  = alert.get("which character", "")
            skill = alert.get("which skill", "flash")
            timer_id = _hero_to_timer_id(hero)
            if timer_id is not None:
                signal = (f"START{timer_id}T" if skill == "teleport" else
                          f"START{timer_id}F" if skill == "flash"    else
                          f"START{timer_id}")
                _send_local_signal(signal)
                print(f"📥 Remote alert: {hero} ({skill}) → {signal}")
        else:
            _send_local_signal(cmd_text)

    except Exception as e:
        print(f"指令處理錯誤: {e}")


# =====================================================================
# 🧠  Voice analysis pipeline  (mirrors test_voice_all_whisper.py)
# =====================================================================
_whisper_model = None
_whisper_lock  = threading.Lock()   # only one transcribe at a time


def _ensure_whisper():
    global _whisper_model
    if _whisper_model is None:
        print("正在載入 Whisper 語音模型...")
        _whisper_model = whisper.load_model("small")
    return _whisper_model


def voice_analysis_pipeline(audio_np: np.ndarray):
    """float32 audio → Whisper → OpenAI → route.

    audio_np: shape (N,), float32, range [-1, 1] — same contract as
    test_voice_all_whisper.py's whisper_model.transcribe() input.
    """
    try:
        t0 = time.time()

        # Whisper — serialise because model is not thread-safe
        with _whisper_lock:
            model = _ensure_whisper()
            result = model.transcribe(audio_np, language="zh", fp16=False)

        text = result["text"].replace(" ", "").strip()

        if not text:
            return

        print(f"[Whisper] {text}  ({time.time() - t0:.1f}s)")

        # Step 1: quick correction + hotkey trigger
        hotkey_text = text
        for wrong, correct in CORRECTIONS.items():
            hotkey_text = hotkey_text.replace(wrong, correct)
        for keyword, key in HOTKEYS.items():
            if keyword in hotkey_text:
                try:
                    import keyboard as kb
                    kb.send(key)
                    print(f"  燈號 {key.upper()} ({keyword})")
                except Exception as e:
                    print(f"  ⚠️ 燈號失敗: {e}")
                break

        # Step 2: OpenAI structured JSON + routing
        t1 = time.time()
        payload = _analyze_voice(text)
        print(f"[AI] {json.dumps(payload, ensure_ascii=False)}  ({time.time() - t1:.1f}s)")

        _route_payload(payload)

    except Exception as e:
        print(f"語音分析錯誤: {e}")


def _extract_json_object(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end   = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start:end + 1]


def _rewrite_with_llm(raw_text: str) -> str:
    """Fallback: rewrite raw text → LOL slang (same as test_voice_all_whisper.py)."""
    if not openai_client:
        return raw_text
    prompt = f"""你現在是一個台灣《英雄聯盟》(LOL) 的高端玩家。你的任務是把語音辨識出來的句子，精簡並轉換成「台服 LOL 遊戲內對話框會出現的極簡術語」。

【核心規則】
1. 極度簡短：能用 2 個字表達，就不要用 3 個字。
2. 絕對安靜：不准有任何解釋、問候語、引號或標點符號，只輸出最終的字。
3. 自動糾錯：語音辨識常有錯字（例如"江山"或"较少"=交閃，"大爷"=打野，"没伞"=沒閃，"小时"=消失，"车队"=撤退），請根據 LOL 情境自動修正。

使用者語音：「{raw_text}」
轉換後的LOL術語："""
    try:
        response = openai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=15,
            temperature=0.1,
            timeout=OPENAI_TIMEOUT_SECONDS,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[LLM fallback 失敗] {e}")
        return raw_text


def _analyze_voice(raw_text: str) -> dict:
    """Call OpenAI to produce structured JSON (same prompt as test_voice_all_whisper.py)."""
    if not openai_client:
        print("[警告] 無 OpenAI client，使用原始文字。")
        return {"kind": "chat", "target": "", "lol_slang_line": raw_text}

    hero_list = "\n".join(f"    {i+1}. {h}" for i, h in enumerate(ENEMIES))

    prompt = f"""你是台灣《英雄聯盟》(LOL) 高端玩家與通訊分類器。請根據「使用者語音轉寫」產出**一個** JSON 物件。

【輸出規則】
1. 只輸出 JSON，不要 markdown、不要說明、不要前後文字。
2. 必須包含鍵：kind, target, lol_slang_line。
3. 欄位：
   - kind：chat | status_report
   - target：這句話的主要目標（英雄/玩家/路線/物件）；若無明確目標請填空字串
   - lol_slang_line：台服極簡術語一行（極短、無多餘標點，符合遊戲內打字習慣）
4. 術語與糾錯沿用台服習慣（江山/较少→交閃語境、大爷→打野、没伞→沒閃、小时→消失、车队→撤退等）。
5. 範例：
   語音轉寫：「阿璃沒有瞬移」 → {{"kind": "status_report", "target": "阿璃", "lol_slang_line": "阿璃沒閃"}}
   語音轉寫：「阿卡麗在上路草叢」 → {{"kind": "chat", "target": "阿卡麗", "lol_slang_line": "阿卡麗在上草"}}
6. 當提到某個敵方英雄的技能/召喚師技能狀態時，kind = status_report。
7. 敵方英雄名單（target 請完全符合此名單中的名字）：
{hero_list}

使用者語音轉寫：「{raw_text}」"""

    try:
        response = openai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0.2,
            timeout=OPENAI_TIMEOUT_SECONDS,
        )
        raw  = (response.choices[0].message.content or "").strip()
        blob = _extract_json_object(raw)
        if not blob:
            raise ValueError("無法從模型回覆中擷取 JSON")
        data = json.loads(blob)
        if not isinstance(data, dict):
            raise ValueError("根節點必須為 JSON 物件")
        return data
    except Exception as e:
        print(f"[結構化 JSON 失敗，改用純文字後備] {e}")
        slang = _rewrite_with_llm(raw_text)
        return {"kind": "chat", "target": "", "lol_slang_line": slang}


def _route_payload(payload: dict):
    kind   = payload.get("kind",           "").lower()
    target = payload.get("target",         "")
    slang  = payload.get("lol_slang_line", "")

    if kind == "status_report" and target:
        skill = _infer_skill(slang)
        alert_json = json.dumps({
            "which character": target,
            "which skill":     skill,
            "lol_slang_line":  slang,
        }, ensure_ascii=False)
        sock.sendto(f"CMD:ENEMY_ALERT:{alert_json}".encode("utf-8"), (RPI_IP, UDP_PORT))

        timer_id = _hero_to_timer_id(target)
        if timer_id is not None:
            signal = (f"START{timer_id}T" if skill == "teleport" else
                      f"START{timer_id}F" if skill == "flash"    else
                      f"START{timer_id}")
            _send_local_signal(signal)
            print(f"📤 Alert: {target} ({skill}) → 倒數 {signal}")

    elif kind == "chat" and slang:
        try:
            import keyboard as kb
            kb.send("enter")
            time.sleep(0.3)
            kb.write(slang, delay=0.05)
            time.sleep(0.2)
            kb.send("enter")
            print(f"💬 遊戲訊息: {slang}")
        except Exception as e:
            print(f"⚠️ 訊息發送失敗: {e}")


def _infer_skill(slang_line: str) -> str:
    s = slang_line.lower()
    if "沒閃" in slang_line or "无闪" in slang_line or "交閃" in slang_line or "no flash" in s:
        return "flash"
    if "沒傳" in slang_line or "沒tp" in s or "no tp" in s or "傳送" in slang_line:
        return "teleport"
    if "沒大" in slang_line or "no r" in s:
        return "ultimate"
    if "沒治" in slang_line:
        return "heal"
    if "沒淨化" in slang_line:
        return "cleanse"
    if "沒點燃" in slang_line or "沒引燃" in slang_line:
        return "ignite"
    if "沒虛弱" in slang_line:
        return "exhaust"
    return "flash"


def _hero_to_timer_id(hero_name: str):
    for i, enemy in enumerate(ENEMIES):
        if enemy == hero_name:
            return i + 1
    return None


def _send_local_signal(signal_text: str):
    """UDP to local Loupedeck plugin on port 5005 → CountdownSignalListener."""
    local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        local.sendto(signal_text.encode("utf-8"), ("127.0.0.1", LOCAL_PLUGIN_PORT))
    finally:
        local.close()


# =====================================================================
# 🏁  Main
# =====================================================================
print(f"\n🌐 連接至 RPi 路由器 ({RPI_IP}:{UDP_PORT})")
print(f"🎮 我: {MY_ROLE} ({MY_HERO})")
print(f"🤝 隊友: {', '.join(ALLIES)}")
print(f"⚔️  敵方: {', '.join(ENEMIES)}")

send_config_to_plugin()


def _preload():
    _ensure_whisper()
    print("✅ Whisper 模型載入完畢！")


# Start background threads
threading.Thread(target=_preload,         daemon=True).start()
threading.Thread(target=ipc_listener,     daemon=True).start()
threading.Thread(target=receive_and_play, daemon=True).start()
threading.Thread(target=vad_loop,         daemon=True).start()

# Open always-on mic stream
mic_stream = sd.InputStream(
    samplerate=RATE,
    channels=CHANNELS,
    dtype="float32",
    blocksize=CHUNK,
    callback=_mic_callback,
)
mic_stream.start()
print("🎤 麥克風已啟用 (always-on, VAD 自動偵測語音)")

try:
    print("\n✅ 系統已啟動！")
    print("┌─────────────────────────────────────────────┐")
    print("│  Creative Console 3×3 配置:                  │")
    print("│  [Ally1] [Ally2] [Ally3]                     │")
    print("│  [Ally4] [Enemy1][Enemy2]                    │")
    print("│  [Enemy3][Enemy4][Enemy5]                    │")
    print("│                                              │")
    print("│  麥克風常開 — 直接說話即可                    │")
    print("│  按盟友按鈕 → 切換語音路由目標               │")
    print("│  說敵方資訊 → VAD 偵測 → Whisper → AI → 倒數 │")
    print("└─────────────────────────────────────────────┘")
    while is_running:
        time.sleep(1.0)
except KeyboardInterrupt:
    is_running = False

# ── Cleanup ──────────────────────────────────────────────────────────
try:
    mic_stream.stop()
    mic_stream.close()
except Exception:
    pass
stream_out.stop()
stream_out.close()
sock.close()
try:
    PID_FILE.unlink(missing_ok=True)
except Exception:
    pass
print("系統已安全關閉。")
