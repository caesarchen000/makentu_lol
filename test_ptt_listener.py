import socket

LOCAL_IPC_PORT = 5006

def listen_ptt_commands():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", LOCAL_IPC_PORT))
    
    print(f"🔌 [PTT 測試監聽器] 已啟動！")
    print(f"正在監聽 Port {LOCAL_IPC_PORT} 來自 Logi Console 的指令...")
    print("💡 請在 Logi Creative Console 上按下配置好的 PTT 按鈕進行測試。")
    print(" (按 Ctrl+C 結束測試)\n")
    
    try:
        while True:
            data, addr = sock.recvfrom(1024)
            msg = data.decode('utf-8').strip()
            
            if msg == "PTT_ALL_START":
                print("🎤 [全頻廣播] 🟢 按鈕已按下！ (發送給 ALL)")
            elif msg == "PTT_JG_START":
                print("🤫 [指定密語] 🟢 按鈕已按下！ (發送給 JG)")
            elif msg == "PTT_STOP":
                print("🔇 [停止發話] 🔴 按鈕已放開！")
            else:
                print(f"❓ 收到未知指令: {msg}")
                
    except KeyboardInterrupt:
        print("\n🛑 測試監聽器已關閉。")
    finally:
        sock.close()

if __name__ == "__main__":
    listen_ptt_commands()
