"""UDP one-liner sender for pip_message_overlay.py (same UTF-8 datagram format)."""

from __future__ import annotations

import socket


def send_overlay_line(host: str, port: int, text: str) -> None:
    """Send one logical line to the PiP overlay listener; no-op if port <= 0."""
    if port <= 0 or not text:
        return
    try:
        line = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
        payload = line.encode("utf-8")
        if len(payload) > 65000:
            payload = payload[:65000]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(payload, (host, port))
    except OSError as e:
        print(f"[PiP overlay] UDP 傳送失敗 ({host}:{port}): {e}")
