import os
import glob
import time
import math
import sys
import tempfile
import cv2
import numpy as np
import threading
import obsws_python as obs
import mss
import ctypes
from ctypes import wintypes

_replay_lock = threading.Lock()
_replay_playing = False

import json

# ==========================================
# ⚙️ 參數設定區 (將由 UI 寫入 tactical_config.json)
# ==========================================
OBS_HOST = "localhost"
OBS_PORT = 4455        
OBS_PASSWORD = "9ECzI8cnMbWWjLx9" 
VIDEO_SAVE_DIR = "D:/obs-studio/video"

try:
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tactical_config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            OBS_HOST = config.get("obs_host", OBS_HOST)
            OBS_PORT = config.get("obs_port", OBS_PORT)
            OBS_PASSWORD = config.get("obs_password", OBS_PASSWORD)
            VIDEO_SAVE_DIR = config.get("video_save_dir", VIDEO_SAVE_DIR)
except Exception as e:
    print(f"[警告] 無法讀取 tactical_config.json，將使用預設 OBS 設定 ({e})")

FORCE_WINDOW_FOREGROUND = True
SINGLE_INSTANCE = True
WINDOW_POS_X = 0
WINDOW_POS_Y = 0

# ==========================================
# 🧪 偵測參數 (黑畫面/去飽和觸發)
# ==========================================
# 如果你發現「死了沒偵測到」，先把 DEBUG_DETECTION 改成 True，看數值再調下面參數
DEBUG_DETECTION = False

# 擷取區塊（以 1920x1080 為例的螢幕中央）。解析度/螢幕縮放不同就要改這裡
MONITOR_REGION = {"top": 340, "left": 760, "width": 400, "height": 400}

# 絕對閾值：平均飽和度 < SAT_ABS_THRESHOLD 判定為死亡（保底）
SAT_ABS_THRESHOLD = 22.0
# 相對閾值：平均飽和度 < (活著基準 * SAT_REL_FACTOR) 也判定死亡（更適應不同畫面）
SAT_REL_FACTOR = 0.45
# 活著基準的 EMA 平滑係數（越小越平滑）
SAT_EMA_ALPHA = 0.08
# Debug 印出頻率（秒）
DEBUG_PRINT_EVERY_S = 0.8

_sat_ema = None
_last_debug_print_t = None

_singleton_mutex_handle = None
_singleton_lock_file_handle = None

def _ensure_single_instance() -> None:
    """Prevent launching multiple copies (which would create multiple windows)."""
    global _singleton_mutex_handle
    if not SINGLE_INSTANCE:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        return

    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    # Unique name per-machine is fine for this use case
    mutex_name = "MakeNTU.death_LOL_alt.singleton"
    handle = kernel32.CreateMutexW(None, True, mutex_name)
    _singleton_mutex_handle = handle

    ERROR_ALREADY_EXISTS = 183
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        print("[系統] 偵測到程式已在執行中，請先關掉舊的再啟動。")
        sys.exit(0)

    # Fallback: file lock (covers edge cases where mutex check fails)
    global _singleton_lock_file_handle
    try:
        lock_path = os.path.join(tempfile.gettempdir(), "MakeNTU.death_LOL_alt.singleton.lock")
        # Create/open lock file
        fh = open(lock_path, "a+", encoding="utf-8")
        _singleton_lock_file_handle = fh
        try:
            import msvcrt  # type: ignore
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except Exception:
            # If we can't lock, just proceed (best effort)
            pass
    except Exception:
        pass

def _try_force_foreground(window_title: str) -> None:
    """Best-effort: bring OpenCV window above games on Windows (won't beat exclusive fullscreen)."""
    if not FORCE_WINDOW_FOREGROUND:
        return

    try:
        user32 = ctypes.windll.user32
    except Exception:
        return

    try:
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
        return

# ==========================================
# 🎬 1. OBS 控制與影片獲取
# ==========================================
def save_obs_replay():
    try:
        client = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD)
        client.save_replay_buffer()
        print("[OBS] 成功送出儲存 30 秒重播指令！")
        try:
            client.disconnect()
        except Exception:
            pass
        
        # 給 OBS 一點時間把影片寫入硬碟（先等一下，再檢查檔案大小是否穩定）
        time.sleep(1.5)
        
        list_of_files = glob.glob(f"{VIDEO_SAVE_DIR}/*.mkv")
        if not list_of_files: list_of_files = glob.glob(f"{VIDEO_SAVE_DIR}/*.mp4")
        if not list_of_files: list_of_files = glob.glob(f"{VIDEO_SAVE_DIR}/*.flv")
            
        if not list_of_files:
            print(f"\n[致命錯誤] 在 {VIDEO_SAVE_DIR} 找不到任何影片！")
            return None
            
        latest_file = max(list_of_files, key=os.path.getctime)
        print(f"[系統] 找到最新重播影片: {latest_file}")

        # 等待檔案寫入完成：檔案大小連續兩次相同才算穩定
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

        # 再用 OpenCV 試開一次，避免抓到「可見但尚未可讀完」的檔案
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

# ==========================================
# 📺 2. 懸浮視窗播放器 (OpenCV) 
# ==========================================
def play_video_in_floating_window(video_path):
    """用 OpenCV 建立一個置頂的懸浮視窗來播放影片，並支援暫停與循環播放尾端 30 秒"""
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

            # 精確 seek：先跳到片段起點，再逐幀 grab() 推進到目標時間
            # 直接 cap.set(POS_MSEC) 只會落在最近的 keyframe，造成時間亂跳
            cap.set(cv2.CAP_PROP_POS_MSEC, segment_start_seconds * 1000.0)
            target_ms = target_pos_seconds * 1000.0
            while True:
                cur_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
                if cur_ms >= target_ms - 100:   # 容許 100 ms 誤差
                    break
                ret = cap.grab()
                if not ret:
                    break
        elif fps > 0:
            target_frame = end_frame - int(remaining_seconds * fps)
            target_frame = max(start_frame, min(end_frame, target_frame))

            # 同樣用逐幀 grab() 精確推進
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

        # 若在暫停狀態下拖曳，立刻讀取一張畫面更新顯示
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
            if end_ms > 0:
                cur_ms = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
                # 修改為 0.1 秒，讓影片可以真正播到 0 秒，避免提早跳轉
                if cur_ms >= (end_ms - 0.1):
                    seek_to_segment_start()
                    play_start_perf = time.perf_counter()
                    paused_total_seconds = 0.0
                    paused_at_perf = None
                    safe_set_trackbar_pos('Remain(s)', segment_seconds)
                    continue
            elif elapsed >= segment_duration_seconds and segment_duration_seconds > 0:
                seek_to_segment_start()
                play_start_perf = time.perf_counter()
                paused_total_seconds = 0.0
                paused_at_perf = None
                safe_set_trackbar_pos('Remain(s)', segment_seconds)
                continue

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
            if current_frame > end_frame:
                seek_to_segment_start()
                play_start_perf = time.perf_counter()
                paused_total_seconds = 0.0
                paused_at_perf = None
                safe_set_trackbar_pos('Remain(s)', segment_seconds)
                continue

            frame_resized = cv2.resize(frame, (799, 469))
            
            # 計算精確剩餘時間並繪製到畫面上
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
    
    # 強制清空 OpenCV 的事件佇列，確保視窗徹底被系統回收
    for _ in range(10):
        cv2.waitKey(10)
        
    print("[播放器] 重播結束，關閉視窗。")

# ==========================================
# 🚀 3. 主程式：Hackathon 備案 (螢幕灰階偵測)
# ==========================================
def trigger_death_replay():
    """觸發死亡重播的完整流程（觸發條件仍由黑畫面偵測決定）"""
    global _replay_playing

    with _replay_lock:
        if _replay_playing:
            print("[系統] 重播播放中，忽略本次觸發。")
            return
        _replay_playing = True

    try:
        print("\n[影像辨識] 偵測到畫面變灰 (玩家陣亡)！啟動重播機制...")
        latest_video = save_obs_replay()
        if latest_video:
            # OpenCV HighGUI 在 Windows 上不穩定於背景執行緒，避免用 Thread 播放
            play_video_in_floating_window(latest_video)
    finally:
        with _replay_lock:
            _replay_playing = False

def is_screen_gray():
    """擷取螢幕畫面，檢查是否變成灰階/去飽和(死亡)"""
    global _sat_ema, _last_debug_print_t
    with mss.MSS() as sct:
        monitor = MONITOR_REGION
        
        # 抓取畫面並轉換成 numpy 陣列
        img = np.array(sct.grab(monitor))
        
        # 將圖片轉換到 HSV 色彩空間
        hsv_img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        hsv_img = cv2.cvtColor(hsv_img, cv2.COLOR_BGR2HSV)
        
        # 計算畫面的平均「飽和度 (Saturation)」(HSV 的 S 通道)
        # 飽和度範圍是 0~255，如果畫面變灰黑白，飽和度會非常低
        avg_saturation = float(np.mean(hsv_img[:, :, 1]))
        avg_value = float(np.mean(hsv_img[:, :, 2]))

        # 用活著狀態建立基準（EMA）
        if _sat_ema is None:
            _sat_ema = avg_saturation
        else:
            _sat_ema = (1.0 - SAT_EMA_ALPHA) * _sat_ema + SAT_EMA_ALPHA * avg_saturation

        rel_threshold = (_sat_ema * SAT_REL_FACTOR) if _sat_ema is not None else SAT_ABS_THRESHOLD
        threshold = min(SAT_ABS_THRESHOLD, rel_threshold)
        is_dead = avg_saturation < threshold

        if DEBUG_DETECTION:
            now = time.time()
            if _last_debug_print_t is None or (now - _last_debug_print_t) >= DEBUG_PRINT_EVERY_S:
                _last_debug_print_t = now
                print(
                    f"[debug] sat={avg_saturation:.1f}  val={avg_value:.1f}  "
                    f"sat_ema={(_sat_ema or 0.0):.1f}  thr={threshold:.1f}  dead={is_dead}"
                )

        return is_dead

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_live_data():
    url = "https://127.0.0.1:2999/liveclientdata/allgamedata"
    try:
        response = requests.get(url, verify=False, timeout=2)
        if response.status_code == 200:
            return response.json()
    except Exception:
        return None

if __name__ == "__main__":
    _ensure_single_instance()
    print("LOL API 死亡偵測器已啟動...")
    prev_dead = None
    while True:
        try:
            data = get_live_data()
            if data:
                # activePlayer 沒有 isDead，需從 allPlayers 裡找自己的資料
                active_player = data.get('activePlayer', {})
                my_name = active_player.get('summonerName', '')
                all_players = data.get('allPlayers', [])

                is_dead = None
                for p in all_players:
                    if p.get('summonerName', '') == my_name:
                        is_dead = p.get('isDead', False)
                        break

                if is_dead is None:
                    # 找不到對應玩家，可能 API 還沒就緒
                    time.sleep(1)
                    continue

                if prev_dead is not None and is_dead != prev_dead:
                    if is_dead:
                        print("你死亡了！")
                        trigger_death_replay()
                    else:
                        print("你復活了！")
                prev_dead = is_dead
            time.sleep(1)
        except KeyboardInterrupt:
            print("\n程式結束")
            break