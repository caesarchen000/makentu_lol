import requests
import json
import time
import urllib3
import os
import shutil
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_live_data():
    url = "https://127.0.0.1:2999/liveclientdata/allgamedata"
    try:
        response = requests.get(url, verify=False, timeout=2)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        # 如果連不上，通常是遊戲還沒完全載入 API 模組
        return None

if __name__ == "__main__":
    info_dir = Path('info')
    info_dir.mkdir(exist_ok=True)
    # --- 清空 info 資料夾，包括所有檔案與資料夾 ---
    for item in info_dir.iterdir():
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)
    print("=== 正在全力偵測遊戲資料 ===")
    while True:
        data = get_live_data()
        if data:
            all_players = data.get('allPlayers', [])
            active_player = data.get('activePlayer', {})
            my_name = active_player.get('summonerName')
            print(f"偵測到 API！目前玩家總數: {len(all_players)}, 你的名字: {my_name}")

            if len(all_players) > 0 and my_name:
                result = {"status": "In Game", "myTeam": [], "theirTeam": []}
                my_team_id = next((p.get('team') for p in all_players if p.get('summonerName') == my_name), None)
                if my_team_id:
                    for p in all_players:
                        p_info = {
                            "isMe": p.get('summonerName') == my_name,
                            "champion": p.get('championName'),
                            "spell1": p.get('summonerSpells', {}).get('summonerSpellOne', {}).get('displayName'),
                            "spell2": p.get('summonerSpells', {}).get('summonerSpellTwo', {}).get('displayName'),
                        }
                        for lane_key in ("teamPosition", "individualPosition", "position", "lane"):
                            if lane_key in p:
                                val = p.get(lane_key)
                                if val is not None and str(val).strip() != "":
                                    p_info[lane_key] = val
                        if p.get('team') == my_team_id:
                            result["myTeam"].append(p_info)
                        else:
                            result["theirTeam"].append(p_info)

                    file_path = 'info/lol_live_info.json'
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(result, f, ensure_ascii=False, indent=4)
                    print(f"✅ 成功！我方 {len(result['myTeam'])} 人，敵方 {len(result['theirTeam'])} 人")
                    print(f"檔案路徑: {os.path.abspath(file_path)}")
                                        # ============ 複製角色&技能圖到 info 下子資料夾 ============
                    IMG_CHAMPION_PATH = Path('image/champion')
                    IMG_SPELL_PATH = Path('image/spell')
                    INFO_CHAMPION_DIR = Path('info/champion')
                    INFO_SPELL_DIR = Path('info/spell')
                    INFO_CHAMPION_DIR.mkdir(parents=True, exist_ok=True)
                    INFO_SPELL_DIR.mkdir(parents=True, exist_ok=True)

                    mine = next((p['champion'] for p in result['myTeam'] if p.get('isMe')), None)
                    champions = [p['champion'] for team in ['myTeam', 'theirTeam'] for p in result[team] if not p.get('isMe')]
                    unique_champions = set(champions)

                    spells = set()
                    for team in ['myTeam', 'theirTeam']:
                        for p in result[team]:
                            spells.add(p['spell1'])
                            spells.add(p['spell2'])

                    # 複製champion圖到 info/champion/
                    for c in unique_champions:
                        src = IMG_CHAMPION_PATH / f'{c}.png'
                        dst = INFO_CHAMPION_DIR / f'{c}.png'
                        if src.exists():
                            shutil.copy(src, dst)
                        else:
                            print(f"[警告] 找不到角色圖片: {src}")
                    # 複製spell圖到 info/spell/
                    for s in spells:
                        src = IMG_SPELL_PATH / f'{s}.png'
                        dst = INFO_SPELL_DIR / f'{s}.png'
                        if src.exists():
                            shutil.copy(src, dst)
                        else:
                            print(f"[警告] 找不到技能圖片: {src}")
                    # ============ 複製結束 ============
                    break
        time.sleep(1)

