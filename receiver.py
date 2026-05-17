#!/usr/bin/env python3
"""
Receiver module - PC端
監聽來自 RPi 的表情訊號，當偵測到 "laugh" 時按下 T 鍵。

Requirements:
    pip install pynput

使用方式：
    python3 receiver.py                    # 預設監聽 0.0.0.0:9999
    python3 receiver.py --port 9999       # 指定監聽端口
"""

import socket
import json
import argparse
import threading
from pynput.keyboard import Controller, Key

keyboard = Controller()

def handle_client(conn, addr):
    """處理來自 RPi 的連接"""
    try:
        print(f"[RECEIVER] Client connected from {addr}")
        data = conn.recv(1024).decode('utf-8')
        print(f"[RECEIVER] Raw data received: {repr(data)}")
        
        if data:
            msg = json.loads(data)
            print(f"[RECEIVER] Parsed JSON: {msg}")
            label = msg.get("expression", "")
            print(f"[RECEIVER] Expression value: {repr(label)}")
            
            if label == "laugh":
                print(f"[ACTION] Pressing 'T' key...")
                keyboard.press('t')
                keyboard.release('t')
                print(f"[ACTION] T key pressed successfully")
            else:
                print(f"[DEBUG] Expression '{label}' does not match 'laugh'")
            
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON decode failed for {addr}: {e}")
        print(f"[ERROR] Raw data was: {repr(data)}")
    except Exception as e:
        print(f"[ERROR] Failed to handle client {addr}: {e}")
    finally:
        conn.close()


def start_receiver(host="0.0.0.0", port=9999):
    """啟動 TCP 伺服器，監聽 RPi 的訊號"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(5)
    
    print(f"[RECEIVER] Listening on {host}:{port}")
    print(f"[RECEIVER] Server started. Tell RPi to connect to this PC's IP:9999")
    print(f"[RECEIVER] Find your PC IP: run 'ipconfig' on Windows")
    print(f"[RECEIVER] Waiting for laugh signals from RPi...")
    
    try:
        while True:
            conn, addr = server.accept()
            # 用執行緒處理每個連接，避免阻塞
            thread = threading.Thread(target=handle_client, args=(conn, addr))
            thread.daemon = True
            thread.start()
    except KeyboardInterrupt:
        print("\n[RECEIVER] Shutting down...")
    finally:
        server.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="PC Receiver - Listen for laugh signals from RPi and press T key"
    )
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="Listen host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9999,
                        help="Listen port (default: 9999)")
    args = parser.parse_args()
    
    start_receiver(host=args.host, port=args.port)
