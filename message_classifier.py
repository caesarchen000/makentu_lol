import argparse
import json
import socket
import time
from pathlib import Path
from typing import Any, Dict

import keyboard


# Network defaults (override with CLI flags if needed).
DEFAULT_RPI_IP = "172.20.10.2"
DEFAULT_RPI_PORT = 5005
DEFAULT_COUNTDOWN_HOST = "127.0.0.1"
DEFAULT_COUNTDOWN_PORT = 5005

# Hero name -> countdown timer id mapping.
# This is the mapping location you can edit later.
HERO_TIMER_MAP = {
    "蓋倫": 1,
    "安妮": 2,
    "好運姐": 3,
    "阿姆姆": 4,
    "雷歐娜": 5,
    "墨菲特": 6,
    "馬爾札哈": 7,
    "艾希": 8,
    "沃維克": 9,
    "索娜": 10,
}

LIVE_INFO_JSON_CANDIDATES = [
    Path(__file__).resolve().parent / "DemoPlugin" / "DemoPlugin" / "images" / "lol_character" / "info" / "lol_live_info.json",
    Path(__file__).resolve().parent / "images" / "lol_character" / "info" / "lol_live_info.json",
]

SKILL_SYNONYMS = {
    "flash": {"flash", "閃現", "没闪", "沒閃"},
    "teleport": {"teleport", "tp", "傳送", "没传", "沒傳"},
    "heal": {"heal", "治療", "治癒"},
    "cleanse": {"cleanse", "淨化"},
    "barrier": {"barrier", "光盾"},
    "ignite": {"ignite", "點燃"},
    "smite": {"smite", "重擊"},
    "ghost": {"ghost", "鬼步"},
    "exhaust": {"exhaust", "虛弱"},
}


def _get_field(payload: Dict[str, Any], key: str, default: str = "") -> str:
    """Read field from top-level first, then payload['message']."""
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()

    message = payload.get("message")
    if isinstance(message, dict):
        nested = message.get(key)
        if isinstance(nested, str) and nested.strip():
            return nested.strip()

    return default


def _infer_skill(payload: Dict[str, Any], slang_line: str) -> str:
    """Infer skill name for status report payload."""
    details = payload.get("details")
    if isinstance(details, dict):
        for key in ("spell", "skill", "which_skill"):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    normalized = slang_line.lower()
    if "沒閃" in slang_line or "无闪" in slang_line or "no flash" in normalized:
        return "flash"
    if "沒大" in slang_line or "no r" in normalized:
        return "ultimate"
    if "沒傳" in slang_line or "沒tp" in normalized or "no tp" in normalized:
        return "teleport"
    if "沒治癒" in slang_line or "沒治" in slang_line:
        return "heal"
    if "沒淨化" in slang_line:
        return "cleanse"
    if "沒虛弱" in slang_line or "无虚弱" in slang_line or "no exhaust" in normalized:
        return "exhaust"
    if "沒光盾" in slang_line or "无光盾" in slang_line or "no barrier" in normalized:
        return "barrier"
    if "沒點燃" in slang_line or "无点燃" in slang_line or "no ignite" in normalized:
        return "ignite"
    if "沒重擊" in slang_line or "无重击" in slang_line or "no smite" in normalized:
        return "smite"
    if "沒鬼步" in slang_line or "无鬼步" in slang_line or "no ghost" in normalized:
        return "ghost"

    return "unknown"


def _normalize_skill_text(text: str) -> str:
    raw = (text or "").strip().lower()
    if not raw:
        return ""

    for canonical, aliases in SKILL_SYNONYMS.items():
        if raw in {a.lower() for a in aliases}:
            return canonical
    return raw


def _resolve_live_info_path() -> Path | None:
    for p in LIVE_INFO_JSON_CANDIDATES:
        if p.exists():
            return p
    return None


def _read_live_slot_map() -> Dict[str, Dict[str, Any]]:
    """Return champion -> slot info based on lol_live_info.json mapping."""
    p = _resolve_live_info_path()
    if p is None:
        return {}

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

    result: Dict[str, Dict[str, Any]] = {}

    for idx, player in enumerate(data.get("theirTeam", [])[:5], start=1):
        champ = str(player.get("champion", "")).strip()
        if champ:
            result[champ] = {
                "timer_id": idx,
                "spell1": str(player.get("spell1", "")).strip(),
                "spell2": str(player.get("spell2", "")).strip(),
                "is_me": False,
            }

    allies = []
    me = None
    for player in data.get("myTeam", []):
        if bool(player.get("isMe")) and me is None:
            me = player
        else:
            allies.append(player)

    for i, player in enumerate(allies[:4], start=6):
        champ = str(player.get("champion", "")).strip()
        if champ:
            result[champ] = {
                "timer_id": i,
                "spell1": str(player.get("spell1", "")).strip(),
                "spell2": str(player.get("spell2", "")).strip(),
                "is_me": False,
            }

    if me:
        champ = str(me.get("champion", "")).strip()
        if champ:
            result[champ] = {
                "timer_id": 10,
                "spell1": str(me.get("spell1", "")).strip(),
                "spell2": str(me.get("spell2", "")).strip(),
                "is_me": True,
            }

    return result


def _resolve_timer_and_skill_channel(target: str, inferred_skill: str) -> tuple[int | None, str | None]:
    """
    Resolve countdown target from live icon mapping.
    Returns (timer_id, channel) where channel is:
      - 'flash' -> slot1 countdown
      - 'teleport' -> slot2 countdown
      - None -> default countdown channel
    """
    target = (target or "").strip()
    inferred = _normalize_skill_text(inferred_skill)
    if not target:
        return None, None

    live = _read_live_slot_map()
    if target in live:
        info = live[target]
        timer_id = int(info["timer_id"])

        s1 = _normalize_skill_text(str(info.get("spell1", "")))
        s2 = _normalize_skill_text(str(info.get("spell2", "")))

        if inferred and inferred == s1:
            return timer_id, "flash"      # top slot
        if inferred and inferred == s2:
            return timer_id, "teleport"   # bottom slot

        # Backward-compatible fallback: direct flash/tp semantics.
        if inferred == "teleport":
            return timer_id, "teleport"
        if inferred == "flash":
            return timer_id, "flash"
        return timer_id, None

    # Fallback to legacy static map if live JSON mapping unavailable.
    timer_id = HERO_TIMER_MAP.get(target)
    if timer_id is None:
        return None, None
    if inferred == "teleport":
        return timer_id, "teleport"
    if inferred == "flash":
        return timer_id, "flash"
    return timer_id, None


def _send_udp_json(host: str, port: int, payload: Dict[str, Any]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(json.dumps(payload, ensure_ascii=False).encode("utf-8"), (host, port))


def _send_countdown_start(
    host: str, port: int, timer_id: int, skill: str | None = None
) -> None:
    """Notify the Logi plugin countdown. Use skill 'teleport' for 傳送, 'flash' or None for 閃現 (legacy STARTn)."""
    if skill == "teleport":
        text = f"START{timer_id}T"
    elif skill == "flash":
        text = f"START{timer_id}F"
    else:
        text = f"START{timer_id}"
    signal = text.encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(signal, (host, port))


def _send_chat_to_game(lol_slang_text: str) -> None:
    # Keep the exact send flow used in test_voice_all_whisper.py (237-241).
    keyboard.send("enter")
    time.sleep(0.3)
    keyboard.write(lol_slang_text, delay=0.05)
    time.sleep(0.2)
    keyboard.send("enter")


def classify_and_route(
    payload: Dict[str, Any],
    rpi_ip: str,
    rpi_port: int,
    countdown_host: str,
    countdown_port: int,
) -> None:
    kind = _get_field(payload, "kind").lower()
    if not kind:
        raise ValueError("JSON missing 'kind'")

    target = _get_field(payload, "target")
    slang = _get_field(payload, "lol_slang_line")

    if kind == "status_report":
        skill = _infer_skill(payload, slang)
        status_payload = {
            "which character": target,
            "which skill": skill,
        }

        # 1) Send status to RPi for all players.
        _send_udp_json(rpi_ip, rpi_port, status_payload)
        print(f"Sent status_report to RPi {rpi_ip}:{rpi_port} -> {status_payload}")

        # 2) Send same JSON to countdown service.
        _send_udp_json(countdown_host, countdown_port, status_payload)
        print(f"Sent countdown payload to {countdown_host}:{countdown_port} -> {status_payload}")

        # 3) Trigger mapped skill cooldown for this target based on live icon mapping.
        timer_id, cd_skill = _resolve_timer_and_skill_channel(target, skill)
        if timer_id is not None and (cd_skill is not None or skill in ("flash", "teleport")):
            _send_countdown_start(
                countdown_host, countdown_port, timer_id, skill=cd_skill
            )
            suffix = "T" if cd_skill == "teleport" else ("F" if cd_skill == "flash" else "")
            print(
                f"Sent countdown START{timer_id}{suffix or ''} (skill={skill}) for target {target}"
            )
        else:
            # Debug fallback: if character/skill mapping is missing, treat as chat.
            if slang:
                print(
                    f"[Fallback->chat] no countdown mapping for target='{target}' skill='{skill}', sending chat."
                )
                _send_chat_to_game(slang)
                print(f"Sent chat to game: {slang}")
            else:
                print(
                    f"No countdown mapping found for target='{target}' and no chat text available."
                )
        return

    if kind == "chat":
        if not slang:
            raise ValueError("chat payload missing 'lol_slang_line'")
        _send_chat_to_game(slang)
        print(f"Sent chat to game: {slang}")
        return

    raise ValueError(f"Unsupported kind: {kind}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify and route LOL message JSON.")
    parser.add_argument("json_file", help="Path to input JSON file.")
    parser.add_argument("--rpi-ip", default=DEFAULT_RPI_IP, help="RPi host.")
    parser.add_argument("--rpi-port", type=int, default=DEFAULT_RPI_PORT, help="RPi UDP port.")
    parser.add_argument("--countdown-host", default=DEFAULT_COUNTDOWN_HOST, help="Countdown host.")
    parser.add_argument("--countdown-port", type=int, default=DEFAULT_COUNTDOWN_PORT, help="Countdown UDP port.")
    args = parser.parse_args()

    with open(args.json_file, "r", encoding="utf-8") as f:
        payload = json.load(f)

    classify_and_route(
        payload=payload,
        rpi_ip=args.rpi_ip,
        rpi_port=args.rpi_port,
        countdown_host=args.countdown_host,
        countdown_port=args.countdown_port,
    )


if __name__ == "__main__":
    main()
