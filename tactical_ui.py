"""
tactical_ui.py — Desktop UI launcher for the tactical voice client.

Screens: Home → Connect (RPi IP/port) → OBS settings → writes tactical_config.json
and starts tactical_client_cloud.py (optional Intro).

Can be compiled to .exe with: pyinstaller --onefile --windowed tactical_ui.py
"""

import atexit
import json
import os
import signal
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont, messagebox
from PIL import Image, ImageTk

# ── Global subprocess tracking ──────────────────────────────────
_child_proc = None
_normal_exit = False  # True when UI exits normally after launching child

def _cleanup_child():
    """Kill the tactical_client_cloud.py subprocess only on abnormal exit."""
    global _child_proc
    if _normal_exit:
        return  # Child should keep running after UI closes normally
    if _child_proc is not None:
        try:
            _child_proc.terminate()
            _child_proc.wait(timeout=3)
        except Exception:
            try:
                _child_proc.kill()
            except Exception:
                pass
        _child_proc = None

atexit.register(_cleanup_child)

def _signal_handler(sig, frame):
    _cleanup_child()
    sys.exit(0)

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "tactical_config.json"

# PiP overlay — must stay aligned with tactical_client_cloud.load_config_or_exit optional keys.
_DEFAULT_PIP_OVERLAY = {
    "pip_overlay_host": "127.0.0.1",
    "pip_overlay_udp_port": 5011,
    "pip_overlay_whisper": True,
    "pip_overlay_autostart": True,
    "pip_overlay_width": 300,
    "pip_overlay_height": 130,
    "pip_overlay_x": 35,
    "pip_overlay_y": 370,
    "pip_overlay_idle_seconds": 5,
}

# ── Shared state ────────────────────────────────────────────────
server_ip = "10.10.31.138"
server_port = 5005

obs_host = "localhost"
obs_port = "4455"
obs_password = "WNokV76EcJbNK26I"
video_save_dir = "D:/obs-studio/video"


class TacticalApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LOL戰術語音系統")
        self.geometry("799x469")
        self.resizable(False, False)
        self.configure(bg="#1a1a2e")

        # Shared fonts
        self.huge_font = tkfont.Font(family="Microsoft JhengHei", size=36, weight="bold")
        self.title_font = tkfont.Font(family="Microsoft JhengHei", size=18, weight="bold")
        self.body_font = tkfont.Font(family="Microsoft JhengHei", size=12)
        self.small_font = tkfont.Font(family="Microsoft JhengHei", size=10)

        # Container for screens
        self.container = tk.Frame(self, bg="#1a1a2e")
        self.container.pack(fill="both", expand=True)

        self.frames = {}
        for ScreenClass in (HomeScreen, ConnectScreen, ObsScreen, IntroScreen):
            frame = ScreenClass(self.container, self)
            self.frames[ScreenClass.__name__] = frame
            frame.grid(row=0, column=0, sticky="nsew")

        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)

        self.show_frame("HomeScreen")

    def show_frame(self, name):
        frame = self.frames[name]
        frame.tkraise()
        if hasattr(frame, "on_show"):
            frame.on_show()


# ═══════════════════════════════════════════════════════════════════
#  Screen 0: Home (Main menu)
# ═══════════════════════════════════════════════════════════════════
class HomeScreen(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg="#1a1a2e")
        self.controller = controller

        # --- Background Image ---
        try:
            img_path = BASE_DIR / "LOL_圖片" / "冠軍造型.jpg"
            if img_path.exists():
                img = Image.open(img_path)
                img = img.resize((799, 469), Image.Resampling.LANCZOS)
                self.bg_image = ImageTk.PhotoImage(img)
                self.bg_label = tk.Label(self, image=self.bg_image)
                self.bg_label.place(x=0, y=0, relwidth=1, relheight=1)
        except Exception as e:
            print(f"Background load error: {e}")

        # Top-left: back/quit
        quit_btn = tk.Button(
            self,
            text="離開",
            font=controller.small_font,
            bg="#333",
            fg="#fff",
            activebackground="#16213e",
            relief="flat",
            bd=0,
            padx=12,
            pady=6,
            command=self.quit_app,
        )
        quit_btn.place(x=18, y=18, anchor="nw")

        # Top-right: feature intro
        intro_btn = tk.Button(
            self,
            text="功能介紹",
            font=controller.small_font,
            bg="#0f3460",
            fg="#fff",
            activebackground="#16213e",
            relief="flat",
            bd=0,
            padx=12,
            pady=6,
            command=self.show_intro,
        )
        intro_btn.place(relx=1.0, x=-18, y=18, anchor="ne")

        title_label = tk.Label(
            self,
            text="LOL戰術語音系統",
            font=controller.huge_font,
            fg="#e94560",
            bg="#1a1a2e",
        )
        title_label.place(relx=0.5, rely=0.5, anchor="center")



        # Middle-bottom: start
        start_btn = tk.Button(
            self,
            text="START ➜",
            font=controller.body_font,
            bg="#e94560",
            fg="#fff",
            activebackground="#c81e45",
            relief="flat",
            bd=0,
            padx=40,
            pady=12,
            command=lambda: controller.show_frame("ConnectScreen"),
        )
        start_btn.place(relx=0.5, rely=0.78, anchor="center")

    def show_intro(self):
        self.controller.show_frame("IntroScreen")

    def quit_app(self):
        if messagebox.askyesno("確認", "要結束 LOL戰術語音系統 嗎？"):
            self.controller.destroy()


# ═══════════════════════════════════════════════════════════════════
#  Screen 1: Connect to server
# ═══════════════════════════════════════════════════════════════════
class ConnectScreen(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg="#1a1a2e")
        self.controller = controller

        back_btn = tk.Button(
            self,
            text="← 返回",
            font=controller.small_font,
            bg="#0f3460",
            fg="#fff",
            activebackground="#16213e",
            relief="flat",
            bd=0,
            padx=12,
            pady=6,
            command=lambda: controller.show_frame("HomeScreen"),
        )
        back_btn.place(x=18, y=18, anchor="nw")

        tk.Label(self, text="🌐 連接伺服器", font=controller.title_font,
                 fg="#e94560", bg="#1a1a2e").pack(pady=(60, 30))

        tk.Label(self, text="RPi 伺服器 IP 位址：", font=controller.body_font,
                 fg="#eee", bg="#1a1a2e").pack()
        self.ip_entry = tk.Entry(self, font=controller.body_font, width=25,
                                 justify="center", bg="#16213e", fg="#fff",
                                 insertbackground="#fff", relief="flat", bd=5)
        self.ip_entry.insert(0, server_ip)
        self.ip_entry.pack(pady=10)

        tk.Label(self, text="Port：", font=controller.body_font,
                 fg="#eee", bg="#1a1a2e").pack()
        self.port_entry = tk.Entry(self, font=controller.body_font, width=10,
                                   justify="center", bg="#16213e", fg="#fff",
                                   insertbackground="#fff", relief="flat", bd=5)
        self.port_entry.insert(0, str(server_port))
        self.port_entry.pack(pady=10)

        self.connect_btn = tk.Button(
            self, text="下一步 ➜", font=controller.body_font,
            bg="#e94560", fg="#fff", activebackground="#c81e45",
            relief="flat", bd=0, padx=30, pady=8,
            command=self.on_connect,
        )
        self.connect_btn.pack(pady=40)

    def on_show(self):
        # Sync UI with latest globals
        self.ip_entry.delete(0, "end")
        self.ip_entry.insert(0, server_ip)
        self.port_entry.delete(0, "end")
        self.port_entry.insert(0, str(server_port))

    def on_connect(self):
        global server_ip, server_port
        server_ip = self.ip_entry.get().strip()
        try:
            server_port = int(self.port_entry.get().strip())
        except ValueError:
            messagebox.showerror("錯誤", "Port 必須是數字")
            return
        if not server_ip:
            messagebox.showerror("錯誤", "請輸入伺服器 IP")
            return
        
        self.controller.show_frame("ObsScreen")


# ═══════════════════════════════════════════════════════════════════
#  Screen: OBS Settings
# ═══════════════════════════════════════════════════════════════════
class ObsScreen(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg="#1a1a2e")
        self.controller = controller

        back_btn = tk.Button(
            self,
            text="← 返回",
            font=controller.small_font,
            bg="#0f3460",
            fg="#fff",
            activebackground="#16213e",
            relief="flat",
            bd=0,
            padx=12,
            pady=6,
            command=lambda: controller.show_frame("ConnectScreen"),
        )
        back_btn.place(x=18, y=18, anchor="nw")

        tk.Label(self, text="🎥 設定 OBS", font=controller.title_font,
                 fg="#e94560", bg="#1a1a2e").pack(pady=(40, 20))

        # OBS Host
        tk.Label(self, text="OBS Host：", font=controller.small_font, fg="#eee", bg="#1a1a2e").pack()
        self.host_entry = tk.Entry(self, font=controller.small_font, width=30, bg="#16213e", fg="#fff", relief="flat", bd=5)
        self.host_entry.pack(pady=5)

        # OBS Port
        tk.Label(self, text="OBS Port：", font=controller.small_font, fg="#eee", bg="#1a1a2e").pack()
        self.port_entry = tk.Entry(self, font=controller.small_font, width=15, bg="#16213e", fg="#fff", relief="flat", bd=5)
        self.port_entry.pack(pady=5)

        # OBS Password
        tk.Label(self, text="OBS WebSocket 密碼：", font=controller.small_font, fg="#eee", bg="#1a1a2e").pack()
        self.pwd_entry = tk.Entry(self, font=controller.small_font, width=30, bg="#16213e", fg="#fff", relief="flat", bd=5, show="*")
        self.pwd_entry.pack(pady=5)

        # Video Save Dir
        tk.Label(self, text="錄影存檔資料夾：", font=controller.small_font, fg="#eee", bg="#1a1a2e").pack()
        self.dir_entry = tk.Entry(self, font=controller.small_font, width=40, bg="#16213e", fg="#fff", relief="flat", bd=5)
        self.dir_entry.pack(pady=5)

        self.start_btn = tk.Button(
            self, text="🚀 啟動系統", font=controller.body_font,
            bg="#e94560", fg="#fff", activebackground="#c81e45",
            relief="flat", bd=0, padx=30, pady=8,
            command=self.on_start,
        )
        self.start_btn.pack(pady=20)

    def on_show(self):
        self.host_entry.delete(0, "end")
        self.host_entry.insert(0, obs_host)
        
        self.port_entry.delete(0, "end")
        self.port_entry.insert(0, obs_port)
        
        self.pwd_entry.delete(0, "end")
        self.pwd_entry.insert(0, obs_password)
        
        self.dir_entry.delete(0, "end")
        self.dir_entry.insert(0, video_save_dir)

    def on_start(self):
        global obs_host, obs_port, obs_password, video_save_dir
        obs_host = self.host_entry.get().strip()
        obs_port = self.port_entry.get().strip()
        obs_password = self.pwd_entry.get().strip()
        video_save_dir = self.dir_entry.get().strip()

        if not obs_port.isdigit():
            messagebox.showerror("錯誤", "OBS Port 必須是數字")
            return
            
        config = {
            "server_ip": server_ip,
            "server_port": server_port,
            "my_role": "PEND",
            "my_hero": "安妮",
            "allies": ["JG", "TOP", "BOT", "SUP"],
            "enemies": ["蓋倫", "好運姐", "阿姆姆", "雷歐娜", "艾希"],
            "obs_host": obs_host,
            "obs_port": int(obs_port),
            "obs_password": obs_password,
            "video_save_dir": video_save_dir,
            **_DEFAULT_PIP_OVERLAY,
        }
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Config saved to {CONFIG_PATH}")

        # Launch tactical_client_cloud.py and track it for cleanup
        global _child_proc, _normal_exit
        client_script = BASE_DIR / "tactical_client_cloud.py"
        _child_proc = subprocess.Popen([sys.executable, str(client_script)], cwd=str(BASE_DIR))
        _normal_exit = True
        messagebox.showinfo("啟動成功", "系統已經啟動，UI 將關閉")
        self.controller.destroy()
# ═══════════════════════════════════════════════════════════════════
#  Screen: IntroScreen
# ═══════════════════════════════════════════════════════════════════
class IntroScreen(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg="#1a1a2e")
        self.controller = controller

        tk.Label(
            self,
            text="功能介紹",
            font=controller.title_font,
            fg="#e94560",
            bg="#1a1a2e",
        ).pack(pady=(40, 10))

        intro_text = (
            "1. 聲控遊戲訊息傳送：對局中按下『SHIFT』開始錄音，再按一下『SHIFT』後結束錄音，"
            "我們的系統會擷取玩家話中的重要對局資訊，如中路沒大招、下路在蹲草等等，"
            "並直接傳送到 LOL 的對話框，省去手動輸入訊息之時間。\n\n"
            "2. 挑戰者技能冷卻顯示：當玩家在錄音中提及某個角色使用了某個挑戰者技能後，"
            "如上路剛交閃現、打野剛傳送等等，Logitech Creative Console 的對應圖示會開始倒數計時，"
            "讓玩家直接看到對方挑戰者技能冷卻時間。\n\n"
            "3. 語音頻道隔離：當玩家在對局中只想跟特定隊友溝通時，只需按下 Logitech Creative Console 上"
            "隊友的對應按鈕，即可切換到與特定隊友的語音頻道，再按一下即可回到全體語音頻道，"
            "避免多人同時溝通造成的訊息混亂。\n\n"
            "4. 死亡即時影片回顧：當玩家死亡後，會自動跳出懸浮視窗撥放死亡前 30 秒的影片，"
            "方便玩家了解死亡原因，提升接下來的對局表現。\n\n"
            "5. 玩家表情發送：當玩家做出較為誇張的表情時，會在 LOL 中自動傳送對應的表情，"
            "省去按表情符號的時間。"
        )

        # 關閉按鈕先 pack 到最底部
        tk.Button(
            self,
            text="關閉",
            font=controller.body_font,
            bg="#0f3460",
            fg="#fff",
            activebackground="#16213e",
            relief="flat",
            bd=0,
            padx=22,
            pady=8,
            command=lambda: controller.show_frame("HomeScreen"),
        ).pack(side="bottom", pady=(10, 18))

        text_frame = tk.Frame(self, bg="#1a1a2e")
        text_frame.pack(padx=18, pady=(0, 10), fill="both", expand=True)

        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side="right", fill="y")

        text = tk.Text(
            text_frame,
            bg="#16213e",
            fg="#eee",
            font=controller.body_font,
            relief="flat",
            bd=0,
            wrap="char",
            yscrollcommand=scrollbar.set,
        )
        text.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=text.yview)

        text.insert("end", intro_text)
        text.configure(state="disabled")


# ═══════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = TacticalApp()
    app.protocol("WM_DELETE_WINDOW", lambda: (_cleanup_child(), app.destroy()))
    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup_child()
