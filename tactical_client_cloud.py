"""
tactical_client_cloud.py — Central tactical voice client.

5v5 LOL voice system:
  - Mic is always captured — UDP voice to RPi is independent of analysis
  - Audio is tagged ALL by default; PTT_ALLY*_TOGGLE buttons change who receives it
  - Tap the Windows key (press then release) to record up to 3s for Whisper / AI
    (ends early on trailing silence); communication stays always-on
  - Whisper + OpenAI run on each completed PTT clip
  - After AI JSON: runs message_classifier.py — RPi UDP, local countdown UDP to Loupedeck;
    chat text goes to optional PiP overlay UDP (pip_message_overlay.py), not League chat when enabled

Usage:
    python tactical_client_cloud.py
"""

import json
import os
import queue
import re
import uuid
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
import math
import glob
import ctypes
from ctypes import wintypes
import obsws_python as obs
import cv2

import numpy as np
import sounddevice as sd
import whisper
from openai import OpenAI

from openai_key_util import load_openai_api_key

from overlay_udp import send_overlay_line

# =====================================================================
# ⚙️  Configuration
# =====================================================================
BASE_DIR    = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "tactical_config.json"


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(1)


def _config_opt_int(raw: dict, key: str, default: int) -> int:
    v = raw.get(key)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _config_opt_float(raw: dict, key: str, default: float) -> float:
    v = raw.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_config_or_exit() -> dict:
    """Read only tactical_config.json — no in-code game defaults (avoids masking user config)."""
    if not CONFIG_PATH.is_file():
        _die(f"[錯誤] 找不到 {CONFIG_PATH}，請先建立或編輯 tactical_config.json 後再啟動。")

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _die(f"[錯誤] 無法讀取 tactical_config.json：{e}")

    if not isinstance(raw, dict):
        _die("[錯誤] tactical_config.json 頂層必須是 JSON 物件。")

    ip = raw.get("server_ip")
    if ip is None or not str(ip).strip():
        _die('[錯誤] tactical_config.json 缺少或無效的 "server_ip"。')

    port_raw = raw.get("server_port")
    if port_raw is None:
        _die('[錯誤] tactical_config.json 缺少 "server_port"。')
    try:
        udp_port = int(port_raw)
    except (TypeError, ValueError):
        _die('[錯誤] tactical_config.json 的 "server_port" 必須為整數。')

    role = raw.get("my_role")
    if role is None or not str(role).strip():
        _die('[錯誤] tactical_config.json 缺少或空的 "my_role"。')

    hero = raw.get("my_hero")
    if hero is None or not str(hero).strip():
        _die('[錯誤] tactical_config.json 缺少或空的 "my_hero"。')

    allies = raw.get("allies")
    if not isinstance(allies, list) or len(allies) < 4:
        _die('[錯誤] tactical_config.json 的 "allies" 必須為長度至少 4 的陣列。')
    allies_out = []
    for i, a in enumerate(allies[:4]):
        s = str(a).strip() if a is not None else ""
        if not s:
            _die(f'[錯誤] tactical_config.json allies[{i}] 不可為空。')
        allies_out.append(s)

    enemies = raw.get("enemies")
    if not isinstance(enemies, list) or len(enemies) < 5:
        _die('[錯誤] tactical_config.json 的 "enemies" 必須為長度至少 5 的陣列。')
    enemies_out = []
    for i, e in enumerate(enemies[:5]):
        s = str(e).strip() if e is not None else ""
        if not s:
            _die(f'[錯誤] tactical_config.json enemies[{i}] 不可為空。')
        enemies_out.append(s)

    pip_port_raw = raw.get("pip_overlay_udp_port")
    pip_overlay_udp_port = 0
    if pip_port_raw is not None:
        try:
            pip_overlay_udp_port = int(pip_port_raw)
        except (TypeError, ValueError):
            pip_overlay_udp_port = 0
    pip_overlay_host = str(raw.get("pip_overlay_host", "127.0.0.1")).strip() or "127.0.0.1"
    if "pip_overlay_whisper" in raw:
        pw = raw.get("pip_overlay_whisper")
        if isinstance(pw, bool):
            pip_overlay_whisper = pw
        else:
            pip_overlay_whisper = str(pw).strip().lower() in ("1", "true", "yes")
    else:
        pip_overlay_whisper = pip_overlay_udp_port > 0

    pa = raw.get("pip_overlay_autostart", True)
    if isinstance(pa, bool):
        pip_overlay_autostart = pa
    elif isinstance(pa, str):
        pip_overlay_autostart = pa.strip().lower() in ("1", "true", "yes")
    else:
        pip_overlay_autostart = True

    # Defaults sized to sit over LoL client chat (bottom-left); tune if resolution/HUD scale differs.
    pip_overlay_width = _config_opt_int(raw, "pip_overlay_width", 300)
    pip_overlay_height = _config_opt_int(raw, "pip_overlay_height", 130)
    pip_overlay_x = _config_opt_int(raw, "pip_overlay_x", 35)
    pip_overlay_y = _config_opt_int(raw, "pip_overlay_y", 370)
    pip_overlay_idle_seconds = _config_opt_float(raw, "pip_overlay_idle_seconds", 5.0)

    return {
        "server_ip":   str(ip).strip(),
        "server_port": udp_port,
        "my_role":     str(role).strip(),
        "my_hero":     str(hero).strip(),
        "allies":      allies_out,
        "enemies":     enemies_out,
        "obs_host":    raw.get("obs_host", "localhost"),
        "obs_port":    int(raw.get("obs_port", 4455)),
        "obs_password": raw.get("obs_password", "9ECzI8cnMbWWjLx9"),
        "video_save_dir": raw.get("video_save_dir", "D:/obs-studio/video"),
        "pip_overlay_host":       pip_overlay_host,
        "pip_overlay_udp_port":   pip_overlay_udp_port,
        "pip_overlay_whisper":    pip_overlay_whisper,
        "pip_overlay_autostart":  pip_overlay_autostart,
        "pip_overlay_width":      pip_overlay_width,
        "pip_overlay_height":     pip_overlay_height,
        "pip_overlay_x":          pip_overlay_x,
        "pip_overlay_y":          pip_overlay_y,
        "pip_overlay_idle_seconds": pip_overlay_idle_seconds,
    }


config = load_config_or_exit()

RPI_IP            = config["server_ip"]
UDP_PORT          = config["server_port"]
LOCAL_IPC_PORT    = 5006   # Loupedeck plugin → this client
LOCAL_PLUGIN_PORT = 5005   # this client → Loupedeck plugin

# =====================================================================
# ⚙️  OBS & Replay config
# =====================================================================
OBS_HOST          = config["obs_host"]
OBS_PORT          = config["obs_port"]
OBS_PASSWORD      = config["obs_password"]
VIDEO_SAVE_DIR    = config["video_save_dir"]

PIP_OVERLAY_HOST    = config["pip_overlay_host"]
PIP_OVERLAY_PORT    = config["pip_overlay_udp_port"]
PIP_OVERLAY_WHISPER = config["pip_overlay_whisper"]
PIP_OVERLAY_AUTOSTART = config["pip_overlay_autostart"]


def _udp_port_available(port: int) -> bool:
    """True if nothing is bound to UDP port (same check as pip_message_overlay bind)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", port))
        s.close()
        return True
    except OSError:
        return False


def _maybe_autostart_pip_overlay() -> None:
    """Spawn pip_message_overlay.py so the Tk window appears without a second manual terminal."""
    if PIP_OVERLAY_PORT <= 0 or not PIP_OVERLAY_AUTOSTART:
        return
    script = BASE_DIR / "pip_message_overlay.py"
    if not script.is_file():
        print(f"[警告] 找不到 {script}，無法自動開啟 PiP 視窗。")
        return
    if not _udp_port_available(PIP_OVERLAY_PORT):
        print(
            f"[PiP] UDP 埠 {PIP_OVERLAY_PORT} 已被占用，略過自動啟動"
            f"（可能已有 pip_message_overlay 在跑）。"
        )
        return
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--udp-port",
                str(PIP_OVERLAY_PORT),
                "--width",
                str(config["pip_overlay_width"]),
                "--height",
                str(config["pip_overlay_height"]),
                "--x",
                str(config["pip_overlay_x"]),
                "--y",
                str(config["pip_overlay_y"]),
                "--idle-seconds",
                str(config["pip_overlay_idle_seconds"]),
            ],
            cwd=str(BASE_DIR),
        )
        print(f"[PiP] 已自動啟動浮動視窗 (子程序 PID {proc.pid})。")
    except OSError as e:
        print(f"[警告] 自動啟動 PiP 視窗失敗: {e}")


if PIP_OVERLAY_PORT > 0:
    print(f"📺 PiP overlay → UDP {PIP_OVERLAY_HOST}:{PIP_OVERLAY_PORT}")
    _maybe_autostart_pip_overlay()
    if not PIP_OVERLAY_AUTOSTART:
        print(
            f"   （pip_overlay_autostart=false，請手動: python pip_message_overlay.py "
            f"--udp-port {PIP_OVERLAY_PORT}）"
        )

FORCE_WINDOW_FOREGROUND = True
WINDOW_POS_X = 0
WINDOW_POS_Y = 0

_replay_lock = threading.Lock()
_replay_playing = False

def _new_provisional_router_role() -> str:
    """Unique id for HELLO before / without lane sync (Z + 7 hex)."""
    return "Z" + uuid.uuid4().hex[:7].upper()


MY_ROLE  = _new_provisional_router_role()
MY_HERO  = config["my_hero"]
ALLIES   = config["allies"]
ENEMIES  = config["enemies"]

CHANNELS = 1
RATE     = 16000
CHUNK    = 512   # ~64 ms per callback block

# =====================================================================
# ⚙️  Win-key PTT capture (tunable)
# =====================================================================
PTT_SPEECH_RMS         = 0.01   # float32 RMS threshold — above this = speaking
PTT_SILENCE_TIMEOUT    = 0.5    # seconds of silence after speech → end clip early
PTT_MIN_DURATION       = 0.5    # seconds — discard shorter clips (noise bursts)
PTT_MAX_DURATION_SEC   = 3.0    # hard cap per tap

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
# ⚙️  message_classifier pipeline (same flow as test_voice_all_whisper.py)
# =====================================================================
PIPELINE_JSON_PATH = BASE_DIR / "pipeline_payload.json"
CLASSIFIER_SCRIPT_PATH = BASE_DIR / "message_classifier.py"
CLASSIFIER_TIMEOUT_SECONDS = 8
IMAGES_ROOT = BASE_DIR / "DemoPlugin" / "DemoPlugin" / "info"


def _list_png_stems(folder: Path):
    if not folder.exists():
        return []
    stems = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() == ".png":
            stems.append(p.stem.strip())
    return sorted(set(s for s in stems if s))


CHARACTER_NAME_LIST = _list_png_stems(IMAGES_ROOT / "champions")
SKILL_NAME_LIST = _list_png_stems(IMAGES_ROOT / "spell")
CHARACTER_NAMES_PROMPT = "、".join(CHARACTER_NAME_LIST) if CHARACTER_NAME_LIST else "（未偵測到角色圖檔）"
SKILL_NAMES_PROMPT = "、".join(SKILL_NAME_LIST) if SKILL_NAME_LIST else "（未偵測到技能圖檔）"

# =====================================================================
# ⚙️  LoL Live Client Data API (port 2999) — same layout as lol_live_info.py
# =====================================================================
LIVE_CLIENT_BASE = "https://127.0.0.1:2999/liveclientdata"
LOL_LIVEINFO_POLL_INTERVAL_SEC = 5.0

OUT_INFO_DIR = IMAGES_ROOT / "lol_character" / "info"
OUT_JSON = OUT_INFO_DIR / "lol_live_info.json"
SRC_CHAMPION_DIR = IMAGES_ROOT / "characters"
SRC_SPELL_DIR = IMAGES_ROOT / "skills"
OUT_CHAMPION_DIR = OUT_INFO_DIR / "champion"
OUT_SPELL_DIR = OUT_INFO_DIR / "spell"

_INSECURE_SSL = ssl.create_default_context()
_INSECURE_SSL.check_hostname = False
_INSECURE_SSL.verify_mode = ssl.CERT_NONE


def _live_client_json_get(url: str, timeout: float = 2.0):
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout, context=_INSECURE_SSL) as resp:
            if resp.status != 200:
                return None
            body = resp.read()
        return json.loads(body.decode("utf-8"))
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        ValueError,
    ):
        return None


def get_gamestats():
    return _live_client_json_get(f"{LIVE_CLIENT_BASE}/gamestats", timeout=2.0)


def get_allgamedata():
    return _live_client_json_get(f"{LIVE_CLIENT_BASE}/allgamedata", timeout=2.0)


def clear_info_dir():
    OUT_INFO_DIR.mkdir(parents=True, exist_ok=True)
    for item in OUT_INFO_DIR.iterdir():
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)


def copy_assets(result: dict):
    OUT_CHAMPION_DIR.mkdir(parents=True, exist_ok=True)
    OUT_SPELL_DIR.mkdir(parents=True, exist_ok=True)

    champions = set()
    spells = set()
    for side in ("myTeam", "theirTeam"):
        for p in result.get(side, []):
            champions.add(p.get("champion", ""))
            spells.add(p.get("spell1", ""))
            spells.add(p.get("spell2", ""))

    for c in champions:
        if not c:
            continue
        src = SRC_CHAMPION_DIR / f"{c}.png"
        dst = OUT_CHAMPION_DIR / f"{c}.png"
        if src.exists():
            shutil.copy(src, dst)
        else:
            print(f"[warn] champion image not found: {src}")

    for s in spells:
        if not s:
            continue
        src_png = SRC_SPELL_DIR / f"{s}.png"
        src_PNG = SRC_SPELL_DIR / f"{s}.PNG"
        src = src_png if src_png.exists() else src_PNG
        dst = OUT_SPELL_DIR / f"{s}.png"
        if src.exists():
            shutil.copy(src, dst)
        else:
            print(f"[warn] spell image not found: {SRC_SPELL_DIR / (s + '.png/.PNG')}")


def _riot_position_to_lane_role(pos) -> str | None:
    """Map Live Client / Riot position strings to router lane keys (MID, JG, TOP, BOT, SUP)."""
    if pos is None:
        return None
    p = str(pos).strip().upper().replace(" ", "").replace("_", "")
    if not p or p in ("NONE", "INVALID"):
        return None
    direct = {
        "TOP": "TOP",
        "JUNGLE": "JG",
        "JUN": "JG",
        "JG": "JG",
        "MIDDLE": "MID",
        "MID": "MID",
        "BOTTOM": "BOT",
        "BOT": "BOT",
        "UTILITY": "SUP",
        "SUPPORT": "SUP",
        "DUO": "SUP",
        "CARRY": "BOT",
    }
    if p in direct:
        return direct[p]
    if "JUNGLE" in p or "JGL" == p:
        return "JG"
    if "MIDDLE" in p or p == "MIDLANE":
        return "MID"
    if "BOTTOM" in p or p == "ADC":
        return "BOT"
    if "UTILITY" in p or "SUPPORT" in p:
        return "SUP"
    return None


def extract_my_lane_role_from_allgamedata(data: dict) -> str | None:
    """Read teamPosition / individualPosition from allPlayers for the active summoner."""
    all_players = data.get("allPlayers") or []
    active = data.get("activePlayer") or {}
    my_name = active.get("summonerName")
    if not my_name or not all_players:
        return None
    me = next((p for p in all_players if p.get("summonerName") == my_name), None)
    if not me:
        return None
    for key in ("teamPosition", "individualPosition", "position", "lane"):
        raw = me.get(key)
        if raw is None:
            continue
        lane = _riot_position_to_lane_role(raw)
        if lane:
            return lane
    return None


def mirror_liveinfo_for_plugin():
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        print("[warn] LOCALAPPDATA not set; skipping mirror to Logi LiveInfo folder")
        return
    dest_root = Path(local) / "Logi" / "LogiPluginService" / "LiveInfo"
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OUT_JSON, dest_root / "lol_live_info.json")
        dst_champ = dest_root / "champion"
        dst_spell = dest_root / "spell"
        if OUT_CHAMPION_DIR.exists():
            shutil.copytree(OUT_CHAMPION_DIR, dst_champ, dirs_exist_ok=True)
        if OUT_SPELL_DIR.exists():
            shutil.copytree(OUT_SPELL_DIR, dst_spell, dirs_exist_ok=True)
        print(f"mirrored live info → {dest_root}")
    except Exception as e:
        print(f"[warn] mirror to LiveInfo failed: {e}")


def update_tactical_config(result: dict, all_gamedata: dict | None = None):
    """Persist roster + lane from Live Client; lane updates my_role so router can switch off provisional Z-id."""
    try:
        config_doc = {}
        if CONFIG_PATH.exists():
            config_doc = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

        for p in result.get("myTeam", []):
            if p.get("isMe"):
                hero = (p.get("champion") or "").strip()
                if hero:
                    config_doc["my_hero"] = hero
                break

        enemies = [
            (p.get("champion") or "").strip()
            for p in result.get("theirTeam", [])
            if (p.get("champion") or "").strip()
        ]
        if enemies:
            config_doc["enemies"] = enemies[:5]

        lane = extract_my_lane_role_from_allgamedata(all_gamedata) if all_gamedata else None
        if lane:
            config_doc["my_role"] = lane
        else:
            # Avoid restoring stale MID/JG from disk when API has no position yet (e.g. ARAM load).
            config_doc["my_role"] = MY_ROLE

        CONFIG_PATH.write_text(
            json.dumps(config_doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"updated tactical_config.json → my_role={config_doc.get('my_role')!r}, "
            f"my_hero={config_doc.get('my_hero')!r}, enemies={config_doc.get('enemies')}"
        )
    except Exception as e:
        print(f"[warn] could not update tactical_config.json: {e}")


def build_liveinfo_result_from_allgamedata(data: dict):
    all_players = data.get("allPlayers", [])
    active_player = data.get("activePlayer", {})
    my_name = active_player.get("summonerName")
    if not all_players or not my_name:
        return None
    my_team_id = next(
        (p.get("team") for p in all_players if p.get("summonerName") == my_name),
        None,
    )
    if not my_team_id:
        return None
    result = {"status": "In Game", "myTeam": [], "theirTeam": []}
    for p in all_players:
        p_info = {
            "isMe": p.get("summonerName") == my_name,
            "champion": p.get("championName"),
            "spell1": p.get("summonerSpells", {})
            .get("summonerSpellOne", {})
            .get("displayName"),
            "spell2": p.get("summonerSpells", {})
            .get("summonerSpellTwo", {})
            .get("displayName"),
        }
        # Lane keys vary by client/API revision; match extract_my_lane_role_from_allgamedata.
        for lane_key in ("teamPosition", "individualPosition", "position", "lane"):
            if lane_key in p:
                val = p.get(lane_key)
                if val is not None and str(val).strip() != "":
                    p_info[lane_key] = val
        side = "myTeam" if p.get("team") == my_team_id else "theirTeam"
        result[side].append(p_info)
    return result


def reload_runtime_config_from_disk():
    """Reload MY_ROLE / MY_HERO / ENEMIES from tactical_config.json; re-HELLO router."""
    global MY_HERO, ENEMIES, MY_ROLE
    if not CONFIG_PATH.is_file():
        return
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    r = raw.get("my_role")
    if r is not None and str(r).strip():
        MY_ROLE = str(r).strip()
    h = raw.get("my_hero")
    if h is not None and str(h).strip():
        MY_HERO = str(h).strip()
    en = raw.get("enemies")
    if isinstance(en, list) and len(en) >= 5:
        enemies_out = []
        ok = True
        for i, e in enumerate(en[:5]):
            s = str(e).strip() if e is not None else ""
            if not s:
                ok = False
                break
            enemies_out.append(s)
        if ok:
            ENEMIES = enemies_out
    register_with_router()


def wait_for_live_client_and_sync() -> bool:
    """Block until 2999 API returns a full roster; writes JSON, assets, config."""
    clear_info_dir()
    poll_only = os.environ.get("LOL_LIVEINFO_POLL_ONLY", "").strip().lower() in ("1", "true", "yes")
    print("=== 等待 LoL Live Client（對局載入後 API 才可用；每 {:.0f}s 重試）===".format(
        LOL_LIVEINFO_POLL_INTERVAL_SEC
    ))
    if poll_only:
        print("(LOL_LIVEINFO_POLL_ONLY：略過 gamestats，僅輪詢 allgamedata)")
    while is_running:
        if not poll_only:
            if get_gamestats() is None:
                time.sleep(LOL_LIVEINFO_POLL_INTERVAL_SEC)
                continue
        data = get_allgamedata()
        if data is None:
            time.sleep(LOL_LIVEINFO_POLL_INTERVAL_SEC)
            continue
        result = build_liveinfo_result_from_allgamedata(data)
        if result:
            OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
            OUT_JSON.write_text(
                json.dumps(result, ensure_ascii=False, indent=4), encoding="utf-8"
            )
            copy_assets(result)
            mirror_liveinfo_for_plugin()
            update_tactical_config(result, data)
            print(f"wrote {OUT_JSON}")
            return True
        time.sleep(LOL_LIVEINFO_POLL_INTERVAL_SEC)
    return False


# =====================================================================
# ⚙️  Global state
# =====================================================================
is_running = True
state_lock = threading.Lock()

# Set after LoL live fetch completes — mic still sends UDP before this.
liveinfo_ready = threading.Event()

# Routing tag — 4-byte UDP header.  ALL by default; changed by ally toggles.
# Written only from ipc_listener thread; read from mic callback (lock-free OK
# because Python bytes assignment is atomic on CPython).
current_tag: bytes = b"ALL "

# Ally channel toggle state
enabled_ally_slots        = set(range(1, len(ALLIES) + 1))
pending_ally_slots        = set(enabled_ally_slots)
selection_window_deadline = 0.0

# Win-key PTT — armed after Win release; mic callback fills ptt_chunks until flush
ptt_armed          = False
ptt_chunks: list   = []
ptt_in_speech      = False
ptt_silence_start  = None  # wall-clock time or None

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


def register_with_router():
    """Send HELLO so RPi maps this UDP endpoint to MY_ROLE (required for whisper / isolated routing)."""
    try:
        sock.sendto(f"HELLO:{MY_ROLE}".encode(), (RPI_IP, UDP_PORT))
        print(f"已向伺服器註冊身分: [{MY_ROLE}] → {RPI_IP}:{UDP_PORT}")
    except OSError as e:
        print(f"[warn] HELLO 傳送失敗: {e}")


register_with_router()

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

def _clear_ptt_session_locked():
    global ptt_armed, ptt_in_speech, ptt_silence_start
    ptt_armed = False
    ptt_chunks.clear()
    ptt_in_speech = False
    ptt_silence_start = None


def _flush_ptt_session(buf: list):
    """Concatenate collected chunks and dispatch to voice_analysis_pipeline."""
    if not buf:
        return
    audio_np = np.concatenate(buf).flatten()
    duration = len(audio_np) / RATE
    if duration < PTT_MIN_DURATION:
        print(f"[PTT] 錄音太短 ({duration:.1f}s)，已忽略")
        return
    print(f"[PTT] 語音 {duration:.1f}s → Whisper")
    threading.Thread(
        target=voice_analysis_pipeline,
        args=(audio_np,),
        daemon=True,
    ).start()


def win_ptt_listener():
    """Wait for Windows key press+release, then arm one PTT capture (if liveinfo ready)."""
    global ptt_armed, ptt_in_speech, ptt_silence_start
    import keyboard as kb

    while is_running:
        try:
            kb.wait("windows")
        except Exception:
            if not is_running:
                break
            time.sleep(0.2)
            continue
        while is_running and kb.is_pressed("windows"):
            time.sleep(0.02)
        time.sleep(0.04)
        with state_lock:
            if not liveinfo_ready.is_set() or ptt_armed:
                continue
            ptt_armed = True
            ptt_chunks.clear()
            ptt_in_speech = False
            ptt_silence_start = None
        print("[PTT] 錄音開始（最多 3 秒，靜音自動結束）")


# =====================================================================
# 🎙️  Always-on mic callback
#
#  Two jobs per chunk:
#    1. UDP relay  → RPi with current_tag (ALL or specific role)
#    2. PTT buffer → when armed, accumulate until max duration or trailing silence
# =====================================================================
def _mic_callback(indata, frames, callback_time, status):
    """sounddevice calls this for every CHUNK frames, always."""
    global ptt_in_speech, ptt_silence_start
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

    # 2. PTT capture — only after LoL live roster sync (same gate as former VAD)
    finalize_buf = None
    with state_lock:
        if not ptt_armed or not liveinfo_ready.is_set():
            return
        ptt_chunks.append(mono.copy())
        rms = float(np.sqrt(np.mean(mono ** 2)))
        dur = len(ptt_chunks) * CHUNK / RATE

        if rms > PTT_SPEECH_RMS:
            ptt_in_speech = True
            ptt_silence_start = None
        elif ptt_in_speech:
            if ptt_silence_start is None:
                ptt_silence_start = time.time()
            elif time.time() - ptt_silence_start >= PTT_SILENCE_TIMEOUT:
                finalize_buf = ptt_chunks.copy()

        if finalize_buf is None and dur >= PTT_MAX_DURATION_SEC:
            finalize_buf = ptt_chunks.copy()

        if finalize_buf is not None:
            _clear_ptt_session_locked()

    if finalize_buf is not None:
        _flush_ptt_session(finalize_buf)


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
# 📡  Audio receiver — collects incoming ally voice from RPi
#
#  Each sender's audio is queued separately so the mixer can blend
#  them into a single output frame.  Writing all streams directly to
#  stream_out caused a machine-gun artifact: with N≥3 people the loop
#  wrote (N-1)× the audio the playback ring-buffer could consume,
#  overflowing it and producing rapid clicking / glitches.
# =====================================================================

# Per-sender queues: role_tag (str) → queue.Queue of bytes (int16 PCM, CHUNK samples)
_audio_queues: dict[str, queue.Queue] = {}
_audio_queues_lock = threading.Lock()
_AUDIO_QUEUE_MAX = 8   # cap per sender (~512 ms); older frames are dropped on overflow


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

            # Normal audio: first 4 bytes = sender role tag, rest = int16 PCM.
            # Push into the per-sender queue for the mixer thread to consume.
            sender_tag = preview
            pcm_bytes  = data[4:]
            with _audio_queues_lock:
                if sender_tag not in _audio_queues:
                    _audio_queues[sender_tag] = queue.Queue(maxsize=_AUDIO_QUEUE_MAX)
                q = _audio_queues[sender_tag]
            try:
                q.put_nowait(pcm_bytes)
            except queue.Full:
                # Drop the oldest frame to make room so latency stays bounded.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(pcm_bytes)
                except queue.Full:
                    pass
        except Exception:
            pass


def audio_mixer_loop():
    """Mix one CHUNK from every active sender and write the blended frame to stream_out.

    stream_out.write() blocks until PortAudio has room for the frame, so this
    loop is naturally clocked at RATE Hz — it cannot write faster than playback
    regardless of how many senders are active.
    """
    silence = bytes(CHUNK * 2)   # int16 → 2 bytes per sample
    while is_running:
        with _audio_queues_lock:
            queues = list(_audio_queues.values())

        mixed: np.ndarray | None = None
        for q in queues:
            try:
                pcm_bytes = q.get_nowait()
            except queue.Empty:
                continue
            chunk_arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.int32)
            if mixed is None:
                mixed = chunk_arr
            elif len(chunk_arr) == len(mixed):
                mixed = mixed + chunk_arr

        if mixed is not None:
            out = np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()
        else:
            out = silence

        try:
            stream_out.write(out)
        except Exception:
            pass


# Lane keyword → canonical lane name (mirrors message_classifier logic).
_LANE_CANON: dict[str, str] = {
    "中路": "MIDDLE", "中單": "MIDDLE",
    "打野": "JUNGLE", "野區": "JUNGLE",
    "上路": "TOP", "上單": "TOP",
    "下路": "BOTTOM", "射手": "BOTTOM",
    "輔助": "UTILITY", "下路輔": "UTILITY",
    "mid": "MIDDLE", "middle": "MIDDLE",
    "jungle": "JUNGLE", "jg": "JUNGLE", "jng": "JUNGLE",
    "bot": "BOTTOM", "bottom": "BOTTOM", "adc": "BOTTOM",
    "sup": "UTILITY", "support": "UTILITY", "utility": "UTILITY",
    "top": "TOP",
}


def _extract_lane_token(raw: str) -> str:
    """Same as message_classifier._extract_lane_token — compact lane.mid → MID."""
    s = str(raw).strip()
    if not s:
        return ""
    for sep in (".", "/", "\\"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    u = s.upper().replace(" ", "").replace("_", "").replace("-", "")
    for prefix in ("LANE", "POSITION", "ROLE", "TEAM", "INDIVIDUAL"):
        if len(u) > len(prefix) and u.startswith(prefix):
            u = u[len(prefix) :]
            break
    return u


def _normalize_live_lane_pos(raw: str) -> str | None:
    """Normalise Riot Live Client lane/position text to the same keys as _LANE_CANON values.

    Must stay in sync with message_classifier._normalize_riot_lane so receiver fallback
    matches when the JSON uses Chinese labels (中路 / 打野 / …).
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.upper() in ("NONE", "INVALID", "LANE_NONE"):
        return None

    zh_exact = {
        "中路": "MIDDLE",
        "中單": "MIDDLE",
        "打野": "JUNGLE",
        "野區": "JUNGLE",
        "上路": "TOP",
        "上單": "TOP",
        "下路": "BOTTOM",
        "射手": "BOTTOM",
        "輔助": "UTILITY",
        "下路輔": "UTILITY",
    }
    if s in zh_exact:
        return zh_exact[s]

    zh_ordered = [
        ("下路輔", "UTILITY"),
        ("中路", "MIDDLE"),
        ("中單", "MIDDLE"),
        ("打野", "JUNGLE"),
        ("野區", "JUNGLE"),
        ("上路", "TOP"),
        ("上單", "TOP"),
        ("下路", "BOTTOM"),
        ("射手", "BOTTOM"),
        ("輔助", "UTILITY"),
    ]
    for needle, lane in zh_ordered:
        if needle in s:
            return lane

    u = _extract_lane_token(s)
    if not u:
        return None

    letter_codes = {
        "MID": "MIDDLE",
        "JG": "JUNGLE",
        "JNG": "JUNGLE",
        "TOP": "TOP",
        "BOT": "BOTTOM",
        "SUP": "UTILITY",
        "UTL": "UTILITY",
    }
    if u in letter_codes:
        return letter_codes[u]

    aliases = {
        "TOP": "TOP",
        "MIDDLE": "MIDDLE",
        "MID": "MIDDLE",
        "MIDDLELANE": "MIDDLE",
        "MIDLANER": "MIDDLE",
        "JUNGLE": "JUNGLE",
        "JUNGLER": "JUNGLE",
        "JG": "JUNGLE",
        "JGL": "JUNGLE",
        "JUN": "JUNGLE",
        "BOTTOM": "BOTTOM",
        "BOT": "BOTTOM",
        "ADC": "BOTTOM",
        "DUO": "BOTTOM",
        "DUOCARRY": "BOTTOM",
        "UTILITY": "UTILITY",
        "SUPPORT": "UTILITY",
        "SUP": "UTILITY",
    }
    if u in aliases:
        return aliases[u]
    if "JUNGLE" in u or u in ("JGL", "JG", "JNG") or u.endswith("JG"):
        return "JUNGLE"
    if "MIDDLE" in u or "MIDLANE" in u or u.endswith("MID"):
        return "MIDDLE"
    if "BOTTOM" in u or u == "ADC" or ("DUO" in u and "CARRY" in u):
        return "BOTTOM"
    if "UTILITY" in u or "SUPPORT" in u:
        return "UTILITY"
    return None


def _resolve_lane_from_live(hero: str, skill: str) -> tuple[int | None, str]:
    """Resolve a lane-role keyword to a timer_id by reading the live JSON directly.

    This does NOT import message_classifier so it is safe to call from any thread
    without risking the keyboard-hook side effects that can raise ImportError.
    """
    canon = _LANE_CANON.get(hero) or _LANE_CANON.get(hero.lower())
    if not canon:
        return None, skill

    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(
            Path(local) / "Logi" / "LogiPluginService" / "LiveInfo" / "lol_live_info.json"
        )
    candidates.append(
        BASE_DIR / "DemoPlugin" / "DemoPlugin" / "info" / "lol_character" / "info" / "lol_live_info.json"
    )

    for p in candidates:
        try:
            if not p.is_file():
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
            for idx, player in enumerate(data.get("theirTeam", [])[:5], 1):
                pos = (
                    player.get("teamPosition") or player.get("individualPosition")
                    or player.get("position") or player.get("lane") or ""
                )
                norm_lane = _normalize_live_lane_pos(str(pos))
                if norm_lane == canon:
                    return idx, skill
        except Exception:
            continue
    return None, skill


def _resolve_remote_alert_timer(hero: str, skill: str) -> tuple[int | None, str]:
    """Resolve timer_id + effective skill for a remote CMD:ENEMY_ALERT.

    Resolution order:
      1. Direct ENEMIES list match (fastest, no I/O).
      2. Lane-role keyword → live JSON lookup (no message_classifier import needed).
      3. Fuzzy champion match via message_classifier (full resolution).
    """
    # 1. Fast path: hero name is in our ENEMIES list.
    timer_id = _hero_to_timer_id(hero)
    if timer_id is not None:
        return timer_id, skill

    # 2. Lane keyword resolution directly from the live JSON.
    tid, resolved_skill = _resolve_lane_from_live(hero, skill)
    if tid is not None:
        return tid, resolved_skill

    # 3. Fuzzy champion / full resolution via message_classifier.
    try:
        import message_classifier as _mc
        tid, cd_skill = _mc._resolve_timer_and_skill_channel(hero, skill)
        return tid, (cd_skill or skill)
    except Exception:
        return None, skill


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

            # Fast path: sender pre-resolved the signal, use it directly.
            # This removes any dependency on the receiver's live-client data.
            pre_signal = alert.get("signal")
            if pre_signal and isinstance(pre_signal, str):
                _send_local_signal(pre_signal)
                print(f"📥 Remote alert: {hero} ({skill}) → {pre_signal}")
                return

            # Fallback: resolve locally (handles old broadcast format).
            timer_id, effective_skill = _resolve_remote_alert_timer(hero, skill)
            if timer_id is not None:
                signal = (f"START{timer_id}T" if effective_skill == "teleport" else
                          f"START{timer_id}F" if effective_skill == "flash"    else
                          f"START{timer_id}")
                _send_local_signal(signal)
                print(f"📥 Remote alert: {hero} ({skill}) → {signal}")
            else:
                print(f"📥 Remote alert received but no timer mapping for: {hero!r}")
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


def _extract_json_blob(text: str):
    """Extract first JSON blob (object or array) from model output — same as test_voice_all_whisper."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

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
    """Accept one object or list of objects — same as test_voice_all_whisper."""
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


def analyze_voice_to_payloads(raw_text: str):
    """OpenAI → JSON list — aligned with test_voice_all_whisper.analyze_voice_to_structured_json."""
    if not openai_client:
        print("[警告] 無 OpenAI client，使用原始文字。")
        return [{"kind": "chat", "target": "", "lol_slang_line": raw_text}]

    hero_list = "\n".join(f"    {i+1}. {h}" for i, h in enumerate(ENEMIES))

    prompt = f"""你是台灣《英雄聯盟》(LOL) 高端玩家與通訊分類器。
請根據「使用者語音轉寫」判斷是單一事件還是多個事件：
- 單一事件：輸出一個 JSON 物件
- 多個事件：輸出 JSON 陣列，每個元素一個事件

【輸出規則】
1. 只輸出 JSON，不要 markdown、不要說明、不要前後文字。
2. 每個事件必須包含鍵：kind, target, lol_slang_line。
3. 欄位：
   - kind：chat | status_report
   - target：這句話的主要目標（英雄名稱，或路線角色語如中路／打野／ADC／下路／輔助／上路），也可為玩家/路線/物件；例如「阿璃」或「中路」；若無明確目標請填空字串
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
7. 本場敵方英雄（status_report 的 target 優先對應此清單）：
{hero_list}
8. 可用技能名稱（優先使用以下名稱做糾錯與歸一化）：
   {SKILL_NAMES_PROMPT}
9. 若出現技能名稱，則一定是 status_report 的 kind。

使用者語音轉寫，注意諧音字不同也可算作同一英雄，例如：
-潘森與攀升
-回家與維迦
-庫奇與酷奇
「{raw_text}」"""

    try:
        response = openai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0.2,
            timeout=OPENAI_TIMEOUT_SECONDS,
        )
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
        print(f"[結構化 JSON 失敗，改用純文字後備] {e}")
        slang = _rewrite_with_llm(raw_text)
        return [{"kind": "chat", "target": "", "lol_slang_line": slang}]


def _infer_skill_from_slang(slang: str) -> str:
    """Quick skill inference from lol_slang_line for the broadcast payload.

    Mirrors the most common cases from message_classifier._infer_skill; the
    full version with details dict is only needed by the local classifier.
    """
    s = slang.lower()
    if "沒閃" in slang or "交閃" in slang or "no flash" in s:
        return "flash"
    if "沒傳" in slang or "沒tp" in s or "no tp" in s:
        return "teleport"
    if "沒大" in slang or "no r" in s:
        return "ultimate"
    if "沒治" in slang:
        return "heal"
    if "沒淨化" in slang:
        return "cleanse"
    if "沒虛弱" in slang or "no exhaust" in s:
        return "exhaust"
    if "沒光盾" in slang or "no barrier" in s:
        return "barrier"
    if "沒點燃" in slang or "no ignite" in s:
        return "ignite"
    if "沒鬼步" in slang or "no ghost" in s:
        return "ghost"
    return "unknown"


def _broadcast_enemy_alert(target: str, skill: str, pre_signal: str | None = None) -> None:
    """Broadcast CMD:ENEMY_ALERT via the router using the already-registered sock.

    message_classifier.py sends this payload in a subprocess with a fresh
    unregistered socket, so the router drops it.  We re-send it here from the
    main registered socket so the router will forward it to all teammates.

    pre_signal is the already-resolved countdown string (e.g. 'START3F') parsed
    directly from message_classifier's stdout.  Embedding it means receivers
    need no resolution step at all — they just forward it to their local plugin.
    """
    if not target:
        return
    try:
        alert: dict = {"which character": target, "which skill": skill}
        if pre_signal:
            alert["signal"] = pre_signal

        packet = ("CMD:ENEMY_ALERT:" + json.dumps(alert, ensure_ascii=False)).encode("utf-8")
        sock.sendto(packet, (RPI_IP, UDP_PORT))
        print(f"📤 廣播技能警報給隊友: {alert}")
    except Exception as e:
        print(f"[warn] 廣播 enemy alert 失敗: {e}")


def run_message_pipeline(payloads):
    """Write pipeline_payload.json and run message_classifier.py — same as test_voice_all_whisper."""
    for idx, payload in enumerate(payloads, start=1):
        PIPELINE_JSON_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  [Pipeline] JSON({idx}/{len(payloads)}) → {PIPELINE_JSON_PATH.name}")

        cmd = [
            sys.executable,
            str(CLASSIFIER_SCRIPT_PATH),
            str(PIPELINE_JSON_PATH),
            "--rpi-ip",
            RPI_IP,
            "--rpi-port",
            str(UDP_PORT),
        ]
        if PIP_OVERLAY_PORT > 0:
            cmd.extend(
                [
                    "--overlay-host",
                    PIP_OVERLAY_HOST,
                    "--overlay-port",
                    str(PIP_OVERLAY_PORT),
                ]
            )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLASSIFIER_TIMEOUT_SECONDS,
        )
        stdout_text = result.stdout.strip()
        if stdout_text:
            print(stdout_text)
        if result.returncode != 0:
            if result.stderr.strip():
                print(result.stderr.strip())
            print(f"⚠️ message_classifier 失敗 (exit {result.returncode})")

        # Broadcast status_reports to teammates via the RPi router.
        # message_classifier runs in a subprocess with an unregistered socket,
        # so its own send to the router is always dropped.  We re-send from the
        # main registered sock here so every connected client receives the alert.
        if payload.get("kind") == "status_report":
            target = str(payload.get("target", "")).strip()
            slang  = str(payload.get("lol_slang_line", "")).strip()
            skill  = _infer_skill_from_slang(slang)

            # Parse the pre-resolved countdown signal directly from message_classifier's
            # printed output (e.g. "Sent countdown START3F (skill=flash) for target 中路").
            # This is the most reliable source — no re-import, no re-resolution needed,
            # and it works correctly for both champion names and lane-role phrases.
            pre_signal: str | None = None
            if stdout_text:
                m = re.search(r"Sent countdown (START\d+[TF]?)\b", stdout_text)
                if m:
                    pre_signal = m.group(1)

            _broadcast_enemy_alert(target, skill, pre_signal=pre_signal)


def voice_analysis_pipeline(audio_np: np.ndarray):
    """float32 audio → Whisper → OpenAI → message_classifier (Loupedeck / RPi / game)."""
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
        if PIP_OVERLAY_PORT > 0 and PIP_OVERLAY_WHISPER:
            send_overlay_line(PIP_OVERLAY_HOST, PIP_OVERLAY_PORT, f"[辨識] {text}")

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

        # Step 2: OpenAI structured JSON → message_classifier.py (matches test_voice_all_whisper)
        if not openai_client:
            print("[警告] 無 OpenAI client，略過結構化路由。")
            return

        t1 = time.time()
        payloads = analyze_voice_to_payloads(text)
        print(f"[AI] {json.dumps(payloads, ensure_ascii=False)}  ({time.time() - t1:.1f}s)")
        run_message_pipeline(payloads)

    except Exception as e:
        print(f"語音分析錯誤: {e}")


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
# 💀  Death Replay System
# =====================================================================
def _try_force_foreground(window_title: str) -> None:
    if not FORCE_WINDOW_FOREGROUND:
        return
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        hwnd = user32.FindWindowW(None, window_title)
        if not hwnd:
            return
        SW_SHOWNORMAL = 1
        HWND_TOPMOST = -1
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_SHOWWINDOW = 0x0040

        user32.ShowWindow(hwnd, SW_SHOWNORMAL)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass

def save_obs_replay():
    try:
        client = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD)
        client.save_replay_buffer()
        print("[OBS] 成功送出儲存 30 秒重播指令！")
        try:
            client.disconnect()
        except Exception:
            pass
        
        time.sleep(1.5)
        
        list_of_files = glob.glob(f"{VIDEO_SAVE_DIR}/*.mkv")
        if not list_of_files: list_of_files = glob.glob(f"{VIDEO_SAVE_DIR}/*.mp4")
        if not list_of_files: list_of_files = glob.glob(f"{VIDEO_SAVE_DIR}/*.flv")
            
        if not list_of_files:
            print(f"\n[致命錯誤] 在 {VIDEO_SAVE_DIR} 找不到任何影片！")
            return None
            
        latest_file = max(list_of_files, key=os.path.getctime)
        print(f"[系統] 找到最新重播影片: {latest_file}")

        stable_checks = 0
        last_size = -1
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                size = os.path.getsize(latest_file)
                if size > 0 and size == last_size:
                    stable_checks += 1
                    if stable_checks >= 2:
                        break
                else:
                    stable_checks = 0
                    last_size = size
            except FileNotFoundError:
                stable_checks = 0
                last_size = -1
            time.sleep(0.5)

        cap = cv2.VideoCapture(latest_file)
        if cap.isOpened():
            cap.release()
        else:
            cap.release()
            time.sleep(1.5)

        return latest_file
    except Exception as e:
        print(f"[錯誤] OBS 連線或存檔失敗: {e}")
        return None

def play_video_in_floating_window(video_path):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print("[錯誤] 無法讀取影片檔")
        return

    window_name = f"Death Replay [pid:{os.getpid()}_{int(time.time())}]"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, 799, 469)
    cv2.setWindowProperty(window_name, cv2.WND_PROP_TOPMOST, 1)
    try:
        cv2.moveWindow(window_name, int(WINDOW_POS_X), int(WINDOW_POS_Y))
    except Exception:
        pass
    _try_force_foreground(window_name)

    def window_alive() -> bool:
        try:
            return cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) >= 1.0
        except Exception:
            return False

    def safe_set_trackbar_pos(name: str, pos: int) -> None:
        try:
            if not window_alive():
                return
            nonlocal _suppress_trackbar_callback
            _suppress_trackbar_callback = True
            cv2.setTrackbarPos(name, window_name, int(pos))
            _suppress_trackbar_callback = False
        except cv2.error:
            try:
                _suppress_trackbar_callback = False
            except Exception:
                pass
            return

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    delay = int(1000 / fps) if fps > 0 else 30

    target_seconds = 30.0
    end_frame = max(0, total_frames - 1)

    duration_seconds = 0.0
    end_ms = 0.0
    try:
        cap.set(cv2.CAP_PROP_POS_AVI_RATIO, 1.0)
        cap.grab()
        end_ms = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
    except Exception:
        end_ms = 0.0

    if end_ms > 0:
        duration_seconds = end_ms / 1000.0
    elif fps > 0 and total_frames > 0:
        duration_seconds = total_frames / fps

    if end_ms > 0:
        segment_duration_seconds = min(target_seconds, duration_seconds) if duration_seconds > 0 else target_seconds
        segment_duration_seconds = max(0.0, float(segment_duration_seconds))
        segment_seconds = int(math.ceil(segment_duration_seconds)) if segment_duration_seconds > 0 else 30
        
        segment_end_seconds = duration_seconds if duration_seconds > 0 else (end_ms / 1000.0)
        segment_start_seconds = max(0.0, segment_end_seconds - segment_duration_seconds)
        start_ms = max(0.0, end_ms - (segment_duration_seconds * 1000.0))
        cap.set(cv2.CAP_PROP_POS_MSEC, start_ms)
        start_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
    else:
        segment_duration_seconds = target_seconds
        segment_seconds = int(math.ceil(segment_duration_seconds))
        segment_end_seconds = target_seconds
        segment_start_seconds = 0.0
        start_frame = max(0, end_frame - int(max(1.0, fps) * 30) + 1) if total_frames > 0 else 0

    def seek_to_segment_start():
        if total_frames > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        elif duration_seconds > 0:
            cap.set(cv2.CAP_PROP_POS_MSEC, segment_start_seconds * 1000.0)
        else:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    seek_to_segment_start()

    play_start_perf = time.perf_counter()
    paused_total_seconds = 0.0
    paused_at_perf = None
    _suppress_trackbar_callback = False

    def on_trackbar(val):
        nonlocal _suppress_trackbar_callback, is_paused
        if _suppress_trackbar_callback:
            return
        remaining_seconds = int(val)

        if duration_seconds > 0:
            target_pos_seconds = max(0.0, duration_seconds - float(remaining_seconds))
            target_pos_seconds = max(segment_start_seconds, min(segment_end_seconds, target_pos_seconds))

            cap.set(cv2.CAP_PROP_POS_MSEC, segment_start_seconds * 1000.0)
            target_ms = target_pos_seconds * 1000.0
            while True:
                cur_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
                if cur_ms >= target_ms - 100:
                    break
                ret = cap.grab()
                if not ret:
                    break
        elif fps > 0:
            target_frame = end_frame - int(remaining_seconds * fps)
            target_frame = max(start_frame, min(end_frame, target_frame))

            cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
            while True:
                cur_f = cap.get(cv2.CAP_PROP_POS_FRAMES)
                if cur_f >= target_frame - 1:
                    break
                ret = cap.grab()
                if not ret:
                    break
        else:
            return

        nonlocal play_start_perf, paused_total_seconds, paused_at_perf
        now = time.perf_counter()
        if duration_seconds > 0:
            cur_ms = cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0
            video_elapsed = max(0.0, (cur_ms / 1000.0) - segment_start_seconds)
        else:
            cur_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
            video_elapsed = max(0.0, (cur_frame - start_frame) / fps) if fps > 0 else 0.0
        play_start_perf = now - paused_total_seconds - video_elapsed
        if paused_at_perf is not None:
            paused_at_perf = now

        if is_paused:
            ret, frame = cap.read()
            if ret:
                frame_resized = cv2.resize(frame, (799, 469))
                if duration_seconds > 0:
                    cur_ms = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
                    rem_s = max(0.0, (end_ms - cur_ms) / 1000.0)
                else:
                    cur_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
                    rem_s = max(0.0, segment_duration_seconds - max(0.0, (cur_frame - start_frame) / fps))
                text = f"Remaining: {rem_s:.1f} s"
                cv2.putText(frame_resized, text, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 3, cv2.LINE_AA)
                cv2.imshow(window_name, frame_resized)

    try:
        cv2.createTrackbar('Remain(s)', window_name, segment_seconds, segment_seconds, on_trackbar)
    except cv2.error:
        pass

    print("[播放器] 開始播放死亡重播...")
    is_paused = False
    last_space_down = False
    last_space_toggle_perf = 0.0
    click_toggle_requested = False

    def on_mouse(event, x, y, flags, param):
        nonlocal click_toggle_requested
        if event == cv2.EVENT_LBUTTONDOWN:
            click_toggle_requested = True

    cv2.setMouseCallback(window_name, on_mouse)

    try:
        import keyboard  # type: ignore
    except Exception:
        keyboard = None

    while cap.isOpened():
        if not window_alive():
            break
        if not is_paused:
            now = time.perf_counter()
            elapsed = now - play_start_perf - paused_total_seconds

            ret, frame = cap.read()
            if not ret:
                cap.release()
                cap = cv2.VideoCapture(video_path)
                if not cap.isOpened():
                    break
                seek_to_segment_start()
                play_start_perf = time.perf_counter()
                paused_total_seconds = 0.0
                paused_at_perf = None
                safe_set_trackbar_pos('Remain(s)', segment_seconds)
                continue

            current_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
            frame_resized = cv2.resize(frame, (799, 469))
            
            if end_ms > 0:
                cur_ms = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
                remaining_time_s = max(0.0, (end_ms - cur_ms) / 1000.0)
                trackbar_val = int(math.ceil(remaining_time_s))
            else:
                remaining_time_s = max(0.0, segment_duration_seconds - elapsed)
                trackbar_val = int(math.ceil(remaining_time_s))
                
            trackbar_val = max(0, min(segment_seconds, trackbar_val))
            
            text = f"Remaining: {remaining_time_s:.1f} s"
            cv2.putText(frame_resized, text, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 3, cv2.LINE_AA)
            cv2.imshow(window_name, frame_resized)

            if (current_frame % 15) == 0:
                _try_force_foreground(window_name)

            safe_set_trackbar_pos('Remain(s)', trackbar_val)

        wait_time = 15 if is_paused else delay
        key = cv2.waitKey(wait_time) & 0xFF

        space_pressed = (key == ord(' '))
        if keyboard is not None:
            s_down = keyboard.is_pressed('space')
            if s_down and not last_space_down:
                space_pressed = True
            last_space_down = s_down

        if click_toggle_requested:
            space_pressed = True
            click_toggle_requested = False

        now = time.perf_counter()
        if space_pressed and (now - last_space_toggle_perf) > 0.25:
            last_space_toggle_perf = now
            is_paused = not is_paused
            if is_paused:
                paused_at_perf = now
            else:
                if paused_at_perf is not None:
                    paused_total_seconds += now - paused_at_perf
                paused_at_perf = None

    cap.release()
    try:
        cv2.destroyWindow(window_name)
    except Exception:
        pass
    
    for _ in range(10):
        cv2.waitKey(10)
        
    print("[播放器] 重播結束，關閉視窗。")

def trigger_death_replay():
    global _replay_playing
    with _replay_lock:
        if _replay_playing:
            print("[系統] 重播播放中，忽略本次觸發。")
            return
        _replay_playing = True
    try:
        print("\n💀 [影像辨識/API] 偵測到玩家陣亡！啟動重播機制...")
        latest_video = save_obs_replay()
        if latest_video:
            play_video_in_floating_window(latest_video)
    finally:
        with _replay_lock:
            _replay_playing = False

def death_monitor_loop():
    print("💀 死亡偵測機制已啟動，等待觸發...")
    prev_dead = None
    while is_running:
        try:
            data = get_allgamedata()
            if data:
                active_player = data.get('activePlayer', {})
                my_name = active_player.get('summonerName', '')
                all_players = data.get('allPlayers', [])

                is_dead = None
                for p in all_players:
                    if p.get('summonerName', '') == my_name:
                        is_dead = p.get('isDead', False)
                        break

                if is_dead is not None:
                    if prev_dead is not None and is_dead != prev_dead:
                        if is_dead:
                            print("💀 你死亡了！")
                            trigger_death_replay()
                        else:
                            print("✨ 你復活了！")
                    prev_dead = is_dead
            time.sleep(1)
        except Exception as e:
            # Silence expected API connection errors during polling
            time.sleep(1)


# =====================================================================
# 🏁  Main
# =====================================================================
print(f"\n🌐 連接至 RPi 路由器 ({RPI_IP}:{UDP_PORT})")
print(f"🎮 我 (暫時路由 ID): {MY_ROLE} — 英雄: {MY_HERO}（對局同步後依 API 註冊線路）")
print(f"🤝 隊友: {', '.join(ALLIES)}")
print(f"⚔️  敵方: {', '.join(ENEMIES)}")

send_config_to_plugin()


def _preload():
    _ensure_whisper()
    print("✅ Whisper 模型載入完畢！")


# Voice to RPi + IPC + RX first; LoL live fetch blocks analysis until in-game.
threading.Thread(target=ipc_listener, daemon=True).start()
threading.Thread(target=receive_and_play, daemon=True).start()
threading.Thread(target=audio_mixer_loop, daemon=True).start()
threading.Thread(target=win_ptt_listener, daemon=True).start()

mic_stream = sd.InputStream(
    samplerate=RATE,
    channels=CHANNELS,
    dtype="float32",
    blocksize=CHUNK,
    callback=_mic_callback,
)
mic_stream.start()
print("🎤 麥克風已啟用 (broadcast 至 RPi；Live Client 同步完成後才可 Win 鍵觸發分析)")

live_sync_ok = False
try:
    live_sync_ok = wait_for_live_client_and_sync()
except KeyboardInterrupt:
    is_running = False
    print("\n已取消等待 Live Client。")

if not is_running or not live_sync_ok:
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
    sys.exit(0)

reload_runtime_config_from_disk()
send_config_to_plugin()
print(f"🎮 場次設定已更新: {MY_HERO} vs {', '.join(ENEMIES)}")

liveinfo_ready.set()
threading.Thread(target=_preload, daemon=True).start()
threading.Thread(target=death_monitor_loop, daemon=True).start()

try:
    print("\n✅ 系統已啟動！")
    print("┌─────────────────────────────────────────────┐")
    print("│  Creative Console 3×3 配置:                  │")
    print("│  [Ally1] [Ally2] [Ally3]                     │")
    print("│  [Ally4] [Enemy1][Enemy2]                    │")
    print("│  [Enemy3][Enemy4][Enemy5]                    │")
    print("│                                              │")
    print("│  麥克風常開 — UDP 語音獨立運作                │")
    print("│  按盟友按鈕 → 切換語音路由目標               │")
    print("│  按一下 Win 鍵 → 錄音(≤3s) → Whisper → AI    │")
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
