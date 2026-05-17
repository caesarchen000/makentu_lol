#!/usr/bin/env python3
"""
Picture-in-Picture message overlay: UDP/TCP lines append to a scrollable window.

- Starts hidden; shows when the first chat line arrives (or any UDP/TCP message).
- After idle_seconds with no new text and no window interaction, hides again.
- Default fully opaque (alpha=1). Window geometry via CLI or env.

Examples::

  python pip_message_overlay.py --udp-port 5011 --width 480 --height 200 --x 100 --y 80
  set PIP_WIDTH=400 && python pip_message_overlay.py

Send test::

  echo -n 'hello team' | nc -u -w1 127.0.0.1 5011
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import select
import socket
import threading
import tkinter as tk
from tkinter import scrolledtext

DEFAULT_UDP_PORT = int(os.environ.get("PIP_UDP_PORT", "5011"))
DEFAULT_TCP_PORT = int(os.environ.get("PIP_TCP_PORT", "5012"))
DEFAULT_ALPHA = float(os.environ.get("PIP_ALPHA", "1.0"))
# Defaults align ~LoL in-game chat box bottom-left (adjust per resolution / HUD scale).
DEFAULT_WIDTH = int(os.environ.get("PIP_WIDTH", "300"))
DEFAULT_HEIGHT = int(os.environ.get("PIP_HEIGHT", "130"))
DEFAULT_POS_X = int(os.environ.get("PIP_X", "35"))
DEFAULT_POS_Y = int(os.environ.get("PIP_Y", "370"))
DEFAULT_IDLE_SEC = float(os.environ.get("PIP_IDLE_SECONDS", "5"))


def _socket_thread(
    q: queue.Queue[str],
    udp_port: int,
    tcp_port: int | None,
) -> None:
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp.bind(("0.0.0.0", udp_port))
    udp.setblocking(False)

    tcp_srv: socket.socket | None = None
    tcp_clients: list[socket.socket] = []
    line_buffers: dict[socket.socket, str] = {}

    if tcp_port is not None:
        tcp_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp_srv.bind(("0.0.0.0", tcp_port))
        tcp_srv.listen(8)
        tcp_srv.setblocking(False)

    bufsize = 65536
    tcp_desc = f", TCP 0.0.0.0:{tcp_port}" if tcp_port is not None else ""
    print(f"pip_message_overlay: UDP 0.0.0.0:{udp_port}{tcp_desc}", flush=True)

    while True:
        read_list: list[socket.socket] = [udp]
        if tcp_srv is not None:
            read_list.append(tcp_srv)
        read_list.extend(tcp_clients)

        readable, _, _ = select.select(read_list, [], [], 1.0)

        for s in readable:
            if s is udp:
                try:
                    data, _addr = udp.recvfrom(bufsize)
                except BlockingIOError:
                    continue
                try:
                    text = data.decode("utf-8", errors="replace")
                except Exception:
                    continue
                q.put(text)
                continue

            if tcp_srv is not None and s is tcp_srv:
                try:
                    conn, _ = tcp_srv.accept()
                except BlockingIOError:
                    continue
                conn.setblocking(False)
                tcp_clients.append(conn)
                line_buffers[conn] = ""
                continue

            try:
                raw = s.recv(bufsize)
            except BlockingIOError:
                continue

            if not raw:
                try:
                    s.close()
                except OSError:
                    pass
                if s in tcp_clients:
                    tcp_clients.remove(s)
                line_buffers.pop(s, None)
                continue

            chunk = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            buf_acc = line_buffers.setdefault(s, "") + chunk
            while "\n" in buf_acc:
                line, buf_acc = buf_acc.split("\n", 1)
                q.put(line + "\n")
            line_buffers[s] = buf_acc


def _ensure_trailing_newline(message: str) -> str:
    if not message:
        return ""
    return message.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="PiP message overlay (show on chat, auto-hide when idle).")
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT, help="UDP listen port.")
    parser.add_argument(
        "--tcp-port",
        type=int,
        nargs="?",
        const=DEFAULT_TCP_PORT,
        default=None,
        help=f"If set without value, listens on {DEFAULT_TCP_PORT}; disable by omitting the flag.",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=DEFAULT_ALPHA,
        help="Window opacity 0–1 (default 1 = fully opaque).",
    )
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="Window width (css pixels).")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="Window height.")
    parser.add_argument("--x", type=int, default=DEFAULT_POS_X, dest="pos_x", help="Window left position.")
    parser.add_argument("--y", type=int, default=DEFAULT_POS_Y, dest="pos_y", help="Window top position.")
    parser.add_argument(
        "--idle-seconds",
        type=float,
        default=DEFAULT_IDLE_SEC,
        dest="idle_seconds",
        help="Hide window after this many seconds with no new message and no interaction.",
    )
    args = parser.parse_args()

    alpha = max(0.15, min(1.0, args.alpha))
    idle_ms = max(100, int(args.idle_seconds * 1000))

    msg_q: queue.Queue[str] = queue.Queue()
    thread = threading.Thread(
        target=_socket_thread,
        args=(msg_q, args.udp_port, args.tcp_port),
        daemon=True,
    )
    thread.start()

    root = tk.Tk()
    root.title("Messages")
    root.attributes("-alpha", alpha)
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    root.geometry(f"{args.width}x{args.height}+{args.pos_x}+{args.pos_y}")

    drag_data: dict[str, int | None] = {"x": None, "y": None}

    def start_drag(ev: tk.Event) -> None:
        drag_data["x"] = ev.x_root
        drag_data["y"] = ev.y_root

    def do_drag(ev: tk.Event) -> None:
        if drag_data["x"] is None or drag_data["y"] is None:
            return
        dx = ev.x_root - drag_data["x"]
        dy = ev.y_root - drag_data["y"]
        drag_data["x"] = ev.x_root
        drag_data["y"] = ev.y_root
        root.geometry(f"+{root.winfo_x() + dx}+{root.winfo_y() + dy}")

    def end_drag(_ev: tk.Event) -> None:
        drag_data["x"] = None
        drag_data["y"] = None

    text = scrolledtext.ScrolledText(
        root,
        wrap=tk.WORD,
        font=("Menlo", 11)
        if sys.platform == "darwin"
        else ("Consolas", 11),
        bg="#111111",
        fg="#eaeaea",
        insertbackground="#eaeaea",
        relief=tk.FLAT,
        borderwidth=8,
        highlightthickness=0,
        state=tk.NORMAL,
        cursor="fleur",
    )
    text.pack(fill=tk.BOTH, expand=True)

    idle_state: dict[str, int | None] = {"job": None}

    def cancel_idle_timer() -> None:
        jid = idle_state["job"]
        if jid is not None:
            try:
                root.after_cancel(jid)
            except tk.TclError:
                pass
            idle_state["job"] = None

    def hide_window() -> None:
        idle_state["job"] = None
        root.withdraw()

    def arm_idle_timer() -> None:
        cancel_idle_timer()
        idle_state["job"] = root.after(idle_ms, hide_window)

    def show_window() -> None:
        root.deiconify()
        try:
            root.lift()
            root.attributes("-topmost", True)
        except tk.TclError:
            pass

    def on_user_activity(_evt: tk.Event | None = None) -> None:
        try:
            if root.winfo_viewable():
                arm_idle_timer()
        except tk.TclError:
            pass

    def on_button1(ev: tk.Event) -> None:
        start_drag(ev)
        on_user_activity(ev)

    def on_motion(ev: tk.Event) -> None:
        do_drag(ev)
        on_user_activity(ev)

    text.bind("<Button-1>", on_button1)
    text.bind("<B1-Motion>", on_motion)
    text.bind("<ButtonRelease-1>", end_drag)
    for seq in ("<Key>", "<MouseWheel>", "<Enter>"):
        text.bind(seq, on_user_activity)
    root.bind("<FocusIn>", on_user_activity)

    root.withdraw()

    def drain_queue() -> None:
        try:
            while True:
                raw = msg_q.get_nowait()
                line = _ensure_trailing_newline(raw)
                if not line.strip("\n"):
                    continue
                show_window()
                text.insert(tk.END, line)
                text.see(tk.END)
                max_lines = 500
                cur = int(str(text.index(tk.END)).split(".")[0])
                if cur > max_lines:
                    text.delete("1.0", f"{cur - max_lines}.0")
                arm_idle_timer()
        except queue.Empty:
            pass
        root.after(50, drain_queue)

    def quit_app(_evt: tk.Event | None = None) -> None:
        root.quit()

    root.bind("<Escape>", quit_app)
    root.bind("<Command-q>", quit_app)
    root.bind("<Command-w>", quit_app)
    root.protocol("WM_DELETE_WINDOW", root.quit)

    drain_queue()
    root.mainloop()


if __name__ == "__main__":
    main()
