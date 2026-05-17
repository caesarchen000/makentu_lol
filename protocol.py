# protocol.py
"""Centralized definition of UDP message formats used by the voice command system.

Message Protocol
================

**Audio packets** (binary):
    First 4 ASCII bytes = target identifier ("ALL ", "JG  ", "MID ", "TOP ", "BOT ", "SUP ")
    Remaining bytes = raw int16 audio samples

**Command packets** (UTF-8 text, always start with ``CMD:``):
    CMD:ENEMY_ALERT:<json>           — broadcast enemy status to all
    CMD:CONFIG:ALLY<n>:<ROLE>        — assign role to ally slot n (1-5)
    CMD:CONFIG:ENEMY<n>:<HERO_NAME>  — assign hero to enemy slot n (1-10)
    CMD:CONFIG:MY_ROLE:<ROLE>        — announce own role
    CMD:COUNTDOWN:<timer_id>         — trigger countdown on a timer slot

**IPC packets** (Loupedeck plugin → Python client, UDP 5006):
    PTT_ALLY<n>_TOGGLE               — toggle ally slot n in/out of whisper set
    PTT_ALL_TOGGLE                   — force broadcast mode (clear whisper set)
    PTT_START                        — mic key pressed (start streaming)
    PTT_STOP                         — mic key released (stop streaming)
"""

from __future__ import annotations

TARGET_LENGTH = 4  # bytes for audio routing header

# ── Lane routing roles (first 4 bytes of mic UDP header; whisper targets) ──
# tactical_client_cloud HELLO may use a provisional Zxxxxxxxx id until Live sync.
ALL_ROLES = ("MID", "JG", "TOP", "BOT", "SUP")

# ── Hero list (matches CountdownTimerCommand order) ──────────────
ALL_HEROES = (
    "蓋倫", "安妮", "好運姐", "阿姆姆", "雷歐娜",
    "墨菲特", "馬爾札哈", "艾希", "沃維克", "索娜",
)

# ── Build / parse audio packets ──────────────────────────────────

def build_audio_packet(target: str, audio_bytes: bytes) -> bytes:
    """Return a complete UDP audio packet with 4-byte routing header."""
    padded = target.ljust(TARGET_LENGTH)[:TARGET_LENGTH]
    return padded.encode("utf-8") + audio_bytes


def parse_audio_packet(data: bytes) -> tuple[str, bytes]:
    """Extract (target, audio_payload) from a received audio packet."""
    if len(data) < TARGET_LENGTH:
        return ("", data)
    target = data[:TARGET_LENGTH].decode("utf-8").strip()
    return target, data[TARGET_LENGTH:]


# ── Build / detect command packets ───────────────────────────────

def build_command(cmd_type: str, payload: str = "") -> bytes:
    """Build a UTF-8 command packet.  E.g. build_command("ENEMY_ALERT", json_str)."""
    msg = f"CMD:{cmd_type}"
    if payload:
        msg += f":{payload}"
    return msg.encode("utf-8")


def is_command(data: bytes) -> bool:
    """True if the packet is a command (not audio)."""
    try:
        return data[:4].decode("utf-8") == "CMD:"
    except (UnicodeDecodeError, IndexError):
        return False


def parse_command(data: bytes) -> tuple[str, str]:
    """Parse ``CMD:<TYPE>:<PAYLOAD>`` → (type, payload).

    Returns ("", "") if the data is not a valid command.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ("", "")
    if not text.startswith("CMD:"):
        return ("", "")
    rest = text[4:]
    if ":" in rest:
        cmd_type, payload = rest.split(":", 1)
        return (cmd_type, payload)
    return (rest, "")
