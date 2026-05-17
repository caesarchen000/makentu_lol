import socket
import time
import threading

UDP_IP = "0.0.0.0"
UDP_PORT = 5005

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))

# 🌟 身份字典：用來對應 "位置代號" -> (IP, Port)
clients = {}
addr_to_role = {}

# Thread lock to protect client dictionaries from race conditions
clients_lock = threading.Lock()

# 紀錄上次印出訊息的時間以避免終端機洗頻
last_print_time = {}

print(f"🚀 RPi 戰術語音路由器已啟動 (Port: {UDP_PORT})")
print("等待各節點連線...")

while True:
    try:
        data, addr = sock.recvfrom(8192)

        # 1. 處理註冊封包 (例如收到 b'HELLO:MID')
        #    同一個 UDP 端點若改身分，先移除舊 role，避免密語路由仍指向過時鍵
        if data.startswith(b'HELLO:'):
            role = data.split(b':')[1].decode('utf-8').strip()
            with clients_lock:
                old_role = addr_to_role.get(addr)
                if old_role is not None and old_role != role:
                    clients.pop(old_role, None)
                clients[role] = addr
                addr_to_role[addr] = role
                online = list(clients.keys())
            print(f"👋 {role} 已連線! 來自: {addr}")
            print(f"   目前線上名單: {online}")
            continue

        # 確保寄件者有註冊過
        with clients_lock:
            sender_role = addr_to_role.get(addr)
        if sender_role is None:
            continue

        # 2. 🌟 處理 CMD: 命令封包 (UTF-8 text, not audio)
        #    CMD: packets are ALWAYS broadcast to ALL clients.
        try:
            text_preview = data[:4].decode('utf-8')
        except UnicodeDecodeError:
            text_preview = ""

        if text_preview == "CMD:":
            cmd_text = data.decode('utf-8')
            now = time.time()
            print_key = f"CMD:{sender_role}"
            if print_key not in last_print_time or (now - last_print_time[print_key] > 1.0):
                # Show a readable preview of the command
                if "ENEMY_ALERT" in cmd_text:
                    print(f"⚔️  [{sender_role}] 敵方資訊廣播: {cmd_text[16:80]}...")
                else:
                    print(f"📨 [{sender_role}] 發送指令: {cmd_text[:80]}...")
                last_print_time[print_key] = now

            # Broadcast to all OTHER clients (skip sender to avoid echo/double-countdown).
            with clients_lock:
                targets = dict(clients)
            for role, client_addr in targets.items():
                if client_addr != addr:
                    try:
                        sock.sendto(data, client_addr)
                    except Exception:
                        pass
            continue

        # 3. 解析語音封包標頭 (前 4 Bytes 是目標)
        target_role = data[:4].decode('utf-8').strip()
        audio_payload = data[4:]

        # 4. 抽換標頭：把標頭改成「發信者的名字」
        sender_header = sender_role.ljust(4, ' ').encode('utf-8')
        forward_data = sender_header + audio_payload

        # 5. 【核心路由邏輯】— 語音按目標路由
        now = time.time()
        print_key = f"{sender_role}->{target_role}"

        if print_key not in last_print_time or (now - last_print_time[print_key] > 1.0):
            if target_role == 'ALL':
                print(f"📡 {sender_role} 正在全頻廣播...")
            else:
                print(f"🤫 {sender_role} 正在密語給 {target_role.strip()}...")
            last_print_time[print_key] = now

        with clients_lock:
            targets = dict(clients)

        if target_role == 'ALL':
            for role, client_addr in targets.items():
                if client_addr != addr:
                    try:
                        sock.sendto(forward_data, client_addr)
                    except Exception:
                        pass

        elif target_role in targets:
            target_addr = targets[target_role]
            sock.sendto(forward_data, target_addr)

    except Exception as e:
        pass

