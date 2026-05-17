import json
import os
import random
import shutil
import time
import urllib3
from pathlib import Path

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"
ROOT_IMAGES_DIR = Path(__file__).resolve().parents[1]  # .../DemoPlugin/DemoPlugin/images
OUT_INFO_DIR = ROOT_IMAGES_DIR / "lol_character" / "info"
OUT_JSON = OUT_INFO_DIR / "lol_live_info.json"
SRC_CHAMPION_DIR = ROOT_IMAGES_DIR / "characters"
SRC_SPELL_DIR = ROOT_IMAGES_DIR / "skills"
OUT_CHAMPION_DIR = OUT_INFO_DIR / "champion"
OUT_SPELL_DIR = OUT_INFO_DIR / "spell"
LIVE_WAIT_TIMEOUT_SEC = 5

# tactical_config.json lives 4 levels up (lol_character → images → DemoPlugin → DemoPlugin → repo root)
REPO_ROOT = Path(__file__).resolve().parents[4]
TACTICAL_CONFIG_PATH = REPO_ROOT / "tactical_config.json"


def get_live_data():
    try:
        response = requests.get(API_URL, verify=False, timeout=2)
        if response.status_code == 200:
            return response.json()
    except Exception:
        return None
    return None


def clear_info_dir():
    OUT_INFO_DIR.mkdir(parents=True, exist_ok=True)
    for item in OUT_INFO_DIR.iterdir():
        if item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)


def copy_assets(result):
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


def mirror_liveinfo_for_plugin():
    """Copy JSON + PNGs to %LocalAppData%\\Logi\\LogiPluginService\\LiveInfo\\

    Logitech/Creative Console loads the plugin from its install folder; it often finds an
    old bundled lol_live_info.json first. The C# side reads the newest file among candidates
    and prefers this mirror path so updates from this script always apply without LOL_REPO_DIR.
    """
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


def update_tactical_config(result: dict):
    """Patch my_hero and enemies in tactical_config.json from real game data.

    Only called for live API data, not for random test data, so the config
    always reflects the actual match.
    """
    try:
        config = {}
        if TACTICAL_CONFIG_PATH.exists():
            config = json.loads(TACTICAL_CONFIG_PATH.read_text(encoding="utf-8"))

        # My champion
        for p in result.get("myTeam", []):
            if p.get("isMe"):
                hero = (p.get("champion") or "").strip()
                if hero:
                    config["my_hero"] = hero
                break

        # Enemy champions (up to 5)
        enemies = [
            (p.get("champion") or "").strip()
            for p in result.get("theirTeam", [])
            if (p.get("champion") or "").strip()
        ]
        if enemies:
            config["enemies"] = enemies[:5]

        TACTICAL_CONFIG_PATH.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"updated tactical_config.json → my_hero={config.get('my_hero')!r}, "
            f"enemies={config.get('enemies')}"
        )
    except Exception as e:
        print(f"[warn] could not update tactical_config.json: {e}")


def list_image_stems(folder: Path):
    stems = []
    for p in folder.glob("*"):
        if p.is_file() and p.suffix.lower() == ".png":
            stems.append(p.stem)
    return sorted(set(stems))


def build_random_result():
    champions = list_image_stems(SRC_CHAMPION_DIR)
    spells = list_image_stems(SRC_SPELL_DIR)

    if not champions or len(champions) < 2:
        raise RuntimeError("Not enough champion images for random fallback.")
    if not spells or len(spells) < 2:
        raise RuntimeError("Not enough skill images for random fallback.")

    # We need 10 champion slots: 5 enemy + 5 myTeam (including me).
    if len(champions) >= 10:
        chosen_champions = random.sample(champions, 10)
    else:
        chosen_champions = [random.choice(champions) for _ in range(10)]

    def random_spells():
        if len(spells) >= 2:
            s1, s2 = random.sample(spells, 2)
        else:
            s1 = s2 = spells[0]
        return s1, s2

    my_team = []
    for idx in range(5):
        s1, s2 = random_spells()
        my_team.append(
            {
                "isMe": idx == 0,
                "champion": chosen_champions[idx],
                "spell1": s1,
                "spell2": s2,
            }
        )

    their_team = []
    for idx in range(5, 10):
        s1, s2 = random_spells()
        their_team.append(
            {
                "isMe": False,
                "champion": chosen_champions[idx],
                "spell1": s1,
                "spell2": s2,
            }
        )

    return {
        "status": "Fallback Test Data",
        "myTeam": my_team,
        "theirTeam": their_team,
    }


if __name__ == "__main__":
    clear_info_dir()
    print("=== waiting for LoL liveclient API ===")
    started_at = time.time()

    while True:
        data = get_live_data()
        if data:
            all_players = data.get("allPlayers", [])
            active_player = data.get("activePlayer", {})
            my_name = active_player.get("summonerName")
            if all_players and my_name:
                my_team_id = next(
                    (p.get("team") for p in all_players if p.get("summonerName") == my_name),
                    None,
                )
                if my_team_id:
                    result = {"status": "In Game", "myTeam": [], "theirTeam": []}
                    for p in all_players:
                        p_info = {
                            "isMe": p.get("summonerName") == my_name,
                            "champion": p.get("championName"),
                            "spell1": p.get("summonerSpells", {}).get("summonerSpellOne", {}).get("displayName"),
                            "spell2": p.get("summonerSpells", {}).get("summonerSpellTwo", {}).get("displayName"),
                        }
                        for lane_key in ("teamPosition", "individualPosition", "position", "lane"):
                            if lane_key in p:
                                val = p.get(lane_key)
                                if val is not None and str(val).strip() != "":
                                    p_info[lane_key] = val
                        side = "myTeam" if p.get("team") == my_team_id else "theirTeam"
                        result[side].append(p_info)

                    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
                    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=4), encoding="utf-8")
                    copy_assets(result)
                    mirror_liveinfo_for_plugin()
                    update_tactical_config(result)
                    print(f"wrote {OUT_JSON}")
                    break
        elif time.time() - started_at >= LIVE_WAIT_TIMEOUT_SEC:
            print(f"[timeout] no live API within {LIVE_WAIT_TIMEOUT_SEC}s, generating random test data...")
            result = build_random_result()
            OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
            OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=4), encoding="utf-8")
            copy_assets(result)
            mirror_liveinfo_for_plugin()
            print(f"wrote fallback test JSON: {OUT_JSON}")
            break
        time.sleep(1)
