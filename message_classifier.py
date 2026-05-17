import argparse
import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Dict

import keyboard

from overlay_udp import send_overlay_line


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

TACTICAL_CONFIG_PATH = Path(__file__).resolve().parent / "tactical_config.json"

SKILL_SYNONYMS = {
    "flash": {"flash", "閃現", "没闪", "沒閃", "沒瞬移", "沒神"},
    "teleport": {"teleport", "tp", "傳送", "没传", "沒傳"},
    "heal": {"heal", "治療", "治癒"},
    "cleanse": {"cleanse", "淨化"},
    "barrier": {"barrier", "光盾"},
    "ignite": {"ignite", "點燃", "點人"},
    "smite": {"smite", "重擊", "中級", "終極"},
    "ghost": {"ghost", "鬼步"},
    "exhaust": {"exhaust", "虛弱"},
}


def _live_info_candidate_paths() -> list[Path]:
    """All lol_live_info.json locations; freshest file wins in _resolve_live_info_path."""
    base = Path(__file__).resolve().parent
    paths: list[Path] = [
        base / "DemoPlugin" / "DemoPlugin" / "images" / "lol_character" / "info" / "lol_live_info.json",
        base / "images" / "lol_character" / "info" / "lol_live_info.json",
        base / "lol_character" / "info" / "lol_live_info.json",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        paths.append(
            Path(local) / "Logi" / "LogiPluginService" / "LiveInfo" / "lol_live_info.json"
        )
    return paths


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
    if "交閃" in slang_line or "交闪现" in slang_line:
        return "flash"
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


def _live_slot_skill_channel(info: Dict[str, Any], inferred: str) -> tuple[int, str | None]:
    timer_id = int(info["timer_id"])
    s1 = _normalize_skill_text(str(info.get("spell1", "")))
    s2 = _normalize_skill_text(str(info.get("spell2", "")))

    if inferred and inferred == s1:
        return timer_id, "flash"
    if inferred and inferred == s2:
        return timer_id, "teleport"
    if inferred == "teleport":
        return timer_id, "teleport"
    if inferred == "flash":
        return timer_id, "flash"
    return timer_id, None


def _fuzzy_live_match(target: str, live: Dict[str, Dict[str, Any]]) -> Dict[str, Any] | None:
    if target in live:
        return live[target]
    best_key = None
    best_score = 0
    for k in live:
        if not k:
            continue
        if len(target) >= 2 and (target in k or k in target):
            score = min(len(k), len(target))
            if score > best_score:
                best_score = score
                best_key = k
    return live.get(best_key) if best_key else None


def _enemy_slot_from_tactical_config(target: str) -> int | None:
    if not TACTICAL_CONFIG_PATH.is_file():
        return None
    try:
        data = json.loads(TACTICAL_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    enemies = data.get("enemies")
    if not isinstance(enemies, list):
        return None
    t = target.strip()
    for i, name in enumerate(enemies[:5]):
        if not isinstance(name, str):
            continue
        n = name.strip()
        if not n:
            continue
        if t == n or (len(t) >= 2 and (t in n or n in t)):
            return i + 1
    return None


def _normalize_skill_text(text: str) -> str:
    raw = (text or "").strip().lower()
    if not raw:
        return ""

    for canonical, aliases in SKILL_SYNONYMS.items():
        if raw in {a.lower() for a in aliases}:
            return canonical
    return raw


def _live_json_has_enemy_lane_hints(data: Dict[str, Any]) -> bool:
    """True if theirTeam rows include any non-empty Riot lane field (lane routing needs this)."""
    for pl in data.get("theirTeam", [])[:5]:
        if not isinstance(pl, dict):
            continue
        for k in ("teamPosition", "individualPosition", "position", "lane"):
            v = pl.get(k)
            if v is None:
                continue
            s = str(v).strip().upper()
            if s and s not in ("NONE", "INVALID", "LANE_NONE"):
                return True
    return False


def _resolve_live_info_path() -> Path | None:
    """Prefer newest file; if multiple candidates, prefer JSON that includes enemy lane fields."""
    scored: list[tuple[int, float, Path]] = []
    for p in _live_info_candidate_paths():
        if not p.is_file():
            continue
        try:
            t = p.stat().st_mtime
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        tier = 1 if _live_json_has_enemy_lane_hints(data) else 0
        scored.append((tier, t, p))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[-1][2]


def _read_live_json_dict() -> Dict[str, Any] | None:
    """Fresh lol_live_info.json dict for lane routing; None if missing/unreadable."""
    p = _resolve_live_info_path()
    if p is None:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _extract_lane_token(raw: str) -> str:
    """Reduce API values like 'lane.mid', 'POSITION_JG', 'mid' to a compact UPPER token."""
    s = str(raw).strip()
    if not s:
        return ""
    # Use last segment for dotted / path-like values (Riot clients vary).
    for sep in (".", "/", "\\"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    u = s.upper().replace(" ", "").replace("_", "").replace("-", "")
    for prefix in ("LANE", "POSITION", "ROLE", "TEAM", "INDIVIDUAL"):
        if len(u) > len(prefix) and u.startswith(prefix):
            u = u[len(prefix) :]
            break
    return u


def _normalize_riot_lane(raw: str) -> str | None:
    """Map Live Client position strings to canonical lane keys (aligned with tactical_client_cloud).

    TW/CN clients often emit *Chinese* labels in teamPosition / lane (e.g. 中路, 打野).
    Riot also emits compact *letter* codes: mid, jg, jng, top, bot, sup (sometimes nested in
    keys like lane.mid). Previously only English was recognised, so lane-based status_report never matched an
    enemy row → no timer_id → no 'Sent countdown' line → broadcast had no signal field.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.upper() in ("NONE", "INVALID", "LANE_NONE"):
        return None

    # Exact Chinese labels (most common from Live Client on zh-TW).
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

    # Substring match for API strings that mix text (longer phrases first).
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

    # Compact letter codes from Live Client (mid / jg / jng / top / bot / sup).
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


def _canonical_lane_from_target_phrase(target: str) -> str | None:
    """Detect lane role phrase (Traditional Chinese / English) in target text."""
    t = (target or "").strip()
    if not t:
        return None
    tl = t.lower()
    # Exact compact codes (same letters Riot uses in JSON).
    short_lane = {
        "mid": "MIDDLE",
        "jg": "JUNGLE",
        "jng": "JUNGLE",
        "top": "TOP",
        "bot": "BOTTOM",
        "sup": "UTILITY",
    }
    if tl in short_lane:
        return short_lane[tl]
    # Longer / specific substrings first (Chinese).
    zh_checks = [
        ("中路", "MIDDLE"),
        ("中單", "MIDDLE"),
        ("打野", "JUNGLE"),
        ("野區", "JUNGLE"),
        ("上路", "TOP"),
        ("上單", "TOP"),
        ("下路輔", "UTILITY"),
        ("下路", "BOTTOM"),
        ("射手", "BOTTOM"),
        ("輔助", "UTILITY"),
    ]
    for needle, lane in zh_checks:
        if needle in t:
            return lane
    eng_ordered = [
        ("middle", "MIDDLE"),
        ("jungle", "JUNGLE"),
        ("bottom", "BOTTOM"),
        ("support", "UTILITY"),
        ("utility", "UTILITY"),
        ("mid", "MIDDLE"),
        ("jng", "JUNGLE"),
        ("jg", "JUNGLE"),
        ("adc", "BOTTOM"),
        ("bot", "BOTTOM"),
        ("sup", "UTILITY"),
        ("top", "TOP"),
    ]
    for needle, lane in eng_ordered:
        if needle in tl:
            return lane
    return None


def _enemy_slot_info_from_lane(data: Dict[str, Any], canonical_lane: str) -> Dict[str, Any] | None:
    """Match enemy timer 1–5 by normalized lane vs theirTeam positions."""
    for idx, player in enumerate(data.get("theirTeam", [])[:5], start=1):
        pos = (
            player.get("teamPosition")
            or player.get("individualPosition")
            or player.get("position")
            or player.get("lane")
            or ""
        )
        n = _normalize_riot_lane(str(pos))
        if n == canonical_lane:
            return {
                "timer_id": idx,
                "spell1": str(player.get("spell1", "")).strip(),
                "spell2": str(player.get("spell2", "")).strip(),
                "is_me": False,
            }
    return None


def _enemy_timer_from_lane_keyword(target: str) -> Dict[str, Any] | None:
    canon = _canonical_lane_from_target_phrase(target)
    if canon is None:
        return None
    data = _read_live_json_dict()
    if not data:
        return None
    return _enemy_slot_info_from_lane(data, canon)


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


def _slot_from_live_by_timer_id(
    live: Dict[str, Dict[str, Any]], timer_id: int, inferred_skill: str
) -> str | None:
    """Search the live slot map by timer_id to find which spell slot inferred_skill occupies.

    Used when champion-name lookup fails but tactical_config / HERO_TIMER_MAP has already
    resolved the timer_id — we still want the correct F/T suffix for the broadcast signal.
    """
    for info in live.values():
        if int(info.get("timer_id", -1)) == timer_id:
            _, channel = _live_slot_skill_channel(info, inferred_skill)
            return channel
    return None


def _resolve_timer_and_skill_channel(target: str, inferred_skill: str) -> tuple[int | None, str | None]:
    """
    Resolve countdown target from lol_live_info (champion match, then lane phrase vs enemy positions),
    then tactical_config enemies, then HERO_TIMER_MAP. Returns (timer_id, cd_channel) where channel is
    'flash' -> STARTnF, 'teleport' -> STARTnT, None -> STARTn legacy.
    """
    target = (target or "").strip()
    inferred = _normalize_skill_text(str(inferred_skill or ""))

    if not target:
        return None, None

    live = _read_live_slot_map()

    info = None
    if live:
        if target in live:
            info = live[target]
        else:
            info = _fuzzy_live_match(target, live)

    if info is not None:
        return _live_slot_skill_channel(info, inferred)

    lane_info = _enemy_timer_from_lane_keyword(target)
    if lane_info is not None:
        return _live_slot_skill_channel(lane_info, inferred)

    slot = _enemy_slot_from_tactical_config(target)
    if slot is not None:
        if inferred == "teleport":
            return slot, "teleport"
        if inferred == "flash":
            return slot, "flash"
        # Champion name lookup failed, but we know the timer_id.
        # Search live map by timer_id to get the correct spell slot for this skill.
        channel = _slot_from_live_by_timer_id(live, slot, inferred)
        return slot, channel

    timer_id = HERO_TIMER_MAP.get(target)
    if timer_id is not None:
        if inferred == "teleport":
            return timer_id, "teleport"
        if inferred == "flash":
            return timer_id, "flash"
        channel = _slot_from_live_by_timer_id(live, timer_id, inferred)
        return timer_id, channel

    return None, None


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


def _send_chat_to_game(
    lol_slang_text: str,
    overlay_host: str = "127.0.0.1",
    overlay_port: int = 0,
) -> None:
    """PiP overlay UDP when overlay_port > 0; else legacy League chat typing."""
    if overlay_port > 0:
        send_overlay_line(overlay_host, overlay_port, lol_slang_text)
        return
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
    overlay_host: str = "127.0.0.1",
    overlay_port: int = 0,
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
        effective = cd_skill
        if timer_id is not None:
            if effective is None and skill == "flash":
                effective = "flash"
            elif effective is None and skill == "teleport":
                effective = "teleport"

        # Send the countdown signal whenever we have a timer_id AND the skill is a
        # recognised summoner spell (not "unknown").  Previously only flash/teleport
        # were allowed, which meant ignite/barrier/ghost/etc. never sent a START
        # signal → the broadcast had no "signal" field → teammates received the
        # wrong spell slot (STARTn defaults to top/flash slot).
        countdown_ok = timer_id is not None and (
            effective is not None or skill in SKILL_SYNONYMS
        )
        if countdown_ok:
            _send_countdown_start(
                countdown_host, countdown_port, timer_id, skill=effective
            )
            suffix = "T" if effective == "teleport" else ("F" if effective == "flash" else "")
            print(
                f"Sent countdown START{timer_id}{suffix or ''} (skill={skill}) for target {target}"
            )
            # When the spell slot couldn't be determined (effective is None), still show
            # the slang text in the overlay so teammates see the announcement.
            if effective is None and slang:
                _send_chat_to_game(slang, overlay_host, overlay_port)
                if overlay_port > 0:
                    print(f"Sent to PiP overlay: {slang}")
        else:
            # No timer_id resolved at all — treat as chat so text reaches the overlay.
            if slang:
                print(
                    f"[Fallback->chat] no countdown mapping for target='{target}' skill='{skill}', sending chat."
                )
                _send_chat_to_game(slang, overlay_host, overlay_port)
                if overlay_port > 0:
                    print(f"Sent to PiP overlay: {slang}")
                else:
                    print(f"Sent chat to game: {slang}")
            else:
                print(
                    f"No countdown mapping found for target='{target}' and no chat text available."
                )
        return

    if kind == "chat":
        if not slang:
            raise ValueError("chat payload missing 'lol_slang_line'")
        _send_chat_to_game(slang, overlay_host, overlay_port)
        if overlay_port > 0:
            print(f"Sent to PiP overlay: {slang}")
        else:
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
    parser.add_argument(
        "--overlay-host",
        default="127.0.0.1",
        help="PiP overlay UDP host (pip_message_overlay.py).",
    )
    parser.add_argument(
        "--overlay-port",
        type=int,
        default=0,
        help="PiP overlay UDP port; 0 = type chat in League instead.",
    )
    args = parser.parse_args()

    with open(args.json_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("pipeline JSON must be an object or an array of objects")
        classify_and_route(
            payload=item,
            rpi_ip=args.rpi_ip,
            rpi_port=args.rpi_port,
            countdown_host=args.countdown_host,
            countdown_port=args.countdown_port,
            overlay_host=args.overlay_host,
            overlay_port=args.overlay_port,
        )


if __name__ == "__main__":
    main()