"""Tkinter desktop tool for injecting simulated IEC-104 monitor values."""

from __future__ import annotations

import json
import math
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from rtu_client import RtuClient

APP_DIR = Path(__file__).resolve().parent
CATALOG_PATH = APP_DIR / "points.json"
SUPPORTED_TYPES = {"SINGLE", "DOUBLE", "STEP", "NORMALIZED", "SCALED", "FLOAT"}


def load_catalog(path: Path) -> tuple[dict, list[dict]]:
    with path.open("r", encoding="utf-8") as catalog_file:
        catalog = json.load(catalog_file)
    connection = catalog.get("connection", {})
    points = catalog.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError("'points' must be a non-empty list")

    seen_ioas: set[int] = set()
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("Each point entry must be an object")
        ioa, kind, name = point.get("ioa"), point.get("type"), point.get("name")
        if not isinstance(ioa, int) or ioa < 0:
            raise ValueError(f"Invalid IOA: {ioa!r}")
        if ioa in seen_ioas:
            raise ValueError(f"Duplicate IOA {ioa}")
        seen_ioas.add(ioa)
        if kind not in SUPPORTED_TYPES:
            raise ValueError(f"IOA {ioa}: unsupported type {kind!r}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"IOA {ioa}: name is required")
        if "min" in point and "max" in point and point["min"] > point["max"]:
            raise ValueError(f"IOA {ioa}: min must not exceed max")
    return connection, points


def normalize_value(point: dict, raw: str) -> str:
    kind = point["type"]
    minimum, maximum = point.get("min"), point.get("max")
    try:
        if kind in {"SINGLE", "DOUBLE", "STEP", "SCALED"}:
            if not re.fullmatch(r"[+-]?\d+", raw.strip()):
                raise ValueError("enter a whole number")
            value = int(raw.strip(), 10)
            if minimum is not None and value < minimum:
                raise ValueError(f"minimum is {minimum}")
            if maximum is not None and value > maximum:
                raise ValueError(f"maximum is {maximum}")
            return str(value)
        value = float(raw.strip())
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        if minimum is not None and value < minimum:
            raise ValueError(f"minimum is {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"maximum is {maximum}")
        return format(value, ".9g")
    except ValueError as error:
        raise ValueError(f"IOA {point['ioa']}: {error}") from error


class PointSimulatorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RTU Point Simulator")
        self.minsize(960, 600)
        try:
            connection_config, self.points = load_catalog(CATALOG_PATH)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            messagebox.showerror("Point catalog error", str(error))
            self.destroy()
            raise SystemExit(1) from error

        self.events: queue.Queue[tuple] = queue.Queue()
        self.client = RtuClient(self.events)
        self.value_vars: dict[int, tk.StringVar] = {}
        self.status_vars: dict[int, tk.StringVar] = {}
        self.send_buttons: dict[int, ttk.Button] = {}
        self.point_rows: dict[int, tuple[tk.Widget, ...]] = {}
        self.pending_ioas: set[int] = set()
        self.connected = False
        self.closing = False
        self.host_var = tk.StringVar(value=connection_config.get("host", "192.168.0.200"))
        self.port_var = tk.StringVar(value=str(connection_config.get("port", 3001)))
        self.connection_var = tk.StringVar(value="Disconnected")
        self.point_search_var = tk.StringVar()
        self.point_count_var = tk.StringVar()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll_events)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        connection = ttk.LabelFrame(root, text="RTU connection", padding=8)
        connection.pack(fill="x")
        ttk.Label(connection, text="IP address").grid(row=0, column=0, sticky="w")
        ttk.Entry(connection, textvariable=self.host_var, width=22).grid(
            row=0, column=1, padx=(6, 14), sticky="w")
        ttk.Label(connection, text="TCP port").grid(row=0, column=2, sticky="w")
        ttk.Entry(connection, textvariable=self.port_var, width=8).grid(
            row=0, column=3, padx=(6, 14), sticky="w")
        self.connect_button = ttk.Button(connection, text="Connect", command=self._connect)
        self.connect_button.grid(row=0, column=4, padx=(0, 12))
        ttk.Label(connection, textvariable=self.connection_var).grid(row=0, column=5, sticky="w")

        points_frame = ttk.LabelFrame(root, text="Monitor points", padding=8)
        points_frame.pack(fill="both", expand=True, pady=(10, 0))
        filter_frame = ttk.Frame(points_frame)
        filter_frame.grid(row=0, column=0, columnspan=6, sticky="ew", pady=(0, 6))
        ttk.Label(filter_frame, text="Filter (IOA, name, type)").pack(side="left")
        search_entry = ttk.Entry(filter_frame, textvariable=self.point_search_var, width=36)
        search_entry.pack(side="left", padx=(8, 12))
        ttk.Label(filter_frame, textvariable=self.point_count_var).pack(side="left")
        self.point_search_var.trace_add("write", self._filter_points)

        headers = ("IOA", "Point", "Type", "Value", "Result", "")
        widths = (9, 42, 15, 18, 25, 10)
        for column, (header, width) in enumerate(zip(headers, widths)):
            ttk.Label(points_frame, text=header, width=width).grid(
                row=1, column=column, padx=3, pady=(0, 5), sticky="w")

        self.points_canvas = tk.Canvas(points_frame, highlightthickness=0, height=360)
        scrollbar = ttk.Scrollbar(points_frame, orient="vertical",
                                  command=self.points_canvas.yview)
        self.points_canvas.configure(yscrollcommand=scrollbar.set)
        self.points_canvas.grid(row=2, column=0, columnspan=6, sticky="nsew")
        scrollbar.grid(row=2, column=6, sticky="ns")
        points_frame.columnconfigure(1, weight=1)
        points_frame.columnconfigure(3, weight=1)
        points_frame.rowconfigure(2, weight=1)

        self.rows_frame = ttk.Frame(self.points_canvas)
        self.rows_frame.columnconfigure(1, weight=1)
        self.rows_frame.columnconfigure(3, weight=1)
        self.rows_window = self.points_canvas.create_window(
            (0, 0), window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>", self._update_scroll_region)
        self.points_canvas.bind("<Configure>", self._resize_rows_frame)
        self.points_canvas.bind("<Enter>", self._enable_mousewheel)
        self.points_canvas.bind("<Leave>", self._disable_mousewheel)

        for point in self.points:
            ioa = point["ioa"]
            self.value_vars[ioa] = tk.StringVar(value=str(point.get("default", "")))
            self.status_vars[ioa] = tk.StringVar(value="Ready")
            widgets = (
                ttk.Label(self.rows_frame, text=str(ioa), width=widths[0]),
                ttk.Label(self.rows_frame, text=point["name"], width=widths[1]),
                ttk.Label(self.rows_frame, text=point["type"], width=widths[2]),
                ttk.Entry(self.rows_frame, textvariable=self.value_vars[ioa],
                          width=widths[3]),
                ttk.Label(self.rows_frame, textvariable=self.status_vars[ioa],
                          width=widths[4]),
            )
            button = ttk.Button(self.rows_frame, text="Send",
                                command=lambda configured_point=point: self._send(configured_point),
                                state="disabled")
            self.send_buttons[ioa] = button
            self.point_rows[ioa] = widgets + (button,)
        self._filter_points()

        log_frame = ttk.LabelFrame(root, text="Activity", padding=8)
        log_frame.pack(fill="x", pady=(10, 0))
        self.log_text = tk.Text(log_frame, height=5, wrap="word", state="disabled")
        self.log_text.pack(fill="x", expand=True)

    def _filter_points(self, *_: object) -> None:
        query = self.point_search_var.get().strip().casefold()
        visible_row = 0
        for point in self.points:
            ioa = point["ioa"]
            widgets = self.point_rows[ioa]
            searchable = f"{ioa} {point['name']} {point['type']}".casefold()
            if query and query not in searchable:
                for widget in widgets:
                    widget.grid_remove()
                continue
            for column, widget in enumerate(widgets):
                widget.grid(row=visible_row, column=column, padx=3, pady=3,
                            sticky="ew" if column in (1, 3) else "w")
            visible_row += 1
        self.point_count_var.set(f"Showing {visible_row} of {len(self.points)} points")
        self.points_canvas.yview_moveto(0)
        self._update_scroll_region()

    def _update_scroll_region(self, _event: tk.Event | None = None) -> None:
        self.points_canvas.configure(scrollregion=self.points_canvas.bbox("all"))

    def _resize_rows_frame(self, event: tk.Event) -> None:
        self.points_canvas.itemconfigure(self.rows_window, width=event.width)

    def _enable_mousewheel(self, _event: tk.Event) -> None:
        self.bind_all("<MouseWheel>", self._on_mousewheel)

    def _disable_mousewheel(self, _event: tk.Event) -> None:
        self.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event: tk.Event) -> None:
        self.points_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _connect(self) -> None:
        if self.connected:
            self.client.disconnect()
            self.connected = False
            self._set_connection_state("Disconnected")
            return
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get().strip())
            if not host or not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid connection", "Enter a host and TCP port (1–65535).")
            return
        self.connect_button.configure(state="disabled")
        self.connection_var.set("Connecting…")
        threading.Thread(target=self.client.connect, args=(host, port), daemon=True).start()

    def _send(self, point: dict) -> None:
        if not self.connected:
            return
        ioa = point["ioa"]
        try:
            value = normalize_value(point, self.value_vars[ioa].get())
        except ValueError as error:
            self.status_vars[ioa].set("Invalid value")
            self._log(str(error))
            return
        self.status_vars[ioa].set("Sending…")
        self.pending_ioas.add(ioa)
        self.send_buttons[ioa].configure(state="disabled")
        command = f"SET {ioa} {point['type']} {value}\n"
        threading.Thread(target=self._send_worker, args=(ioa, command), daemon=True).start()

    def _send_worker(self, ioa: int, command: str) -> None:
        try:
            self.client.send_line(command)
            self.events.put(("sent", ioa, command.rstrip()))
        except OSError as error:
            self.events.put(("send_error", ioa, str(error)))

    def _poll_events(self) -> None:
        if self.closing:
            return
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "connected":
                self.connected = True
                self._set_connection_state(f"Connected to {event[1]}:{event[2]}")
                self._log(f"Connected to {event[1]}:{event[2]}")
            elif kind == "connect_error":
                self.connected = False
                self._set_connection_state("Disconnected")
                self._log(f"Connection failed: {event[1]}")
            elif kind == "disconnected":
                self.connected = False
                self.pending_ioas.clear()
                self._set_connection_state("Disconnected")
                self._log("RTU connection closed")
            elif kind == "line":
                self._handle_response(event[1])
            elif kind == "sent":
                self._log(f"TX {event[2]}")
            elif kind == "send_error":
                self.pending_ioas.discard(event[1])
                self.status_vars[event[1]].set("Send failed")
                self._log(f"IOA {event[1]} send failed: {event[2]}")
                self._enable_send_buttons()
        self.after(100, self._poll_events)

    def _handle_response(self, line: str) -> None:
        self._log(f"RX {line}")
        success = re.match(r"^OK\s+(\d+)(?:\s|$)", line)
        failure = re.match(r"^ERR(?:\s+(\d+))?(?:\s+(.*))?$", line)
        if success:
            ioa = int(success.group(1))
            self.pending_ioas.discard(ioa)
            if ioa in self.status_vars:
                self.status_vars[ioa].set("Accepted")
        elif failure:
            ioa_text, reason = failure.groups()
            if ioa_text:
                ioa = int(ioa_text)
                self.pending_ioas.discard(ioa)
                if ioa in self.status_vars:
                    self.status_vars[ioa].set("Rejected")
            else:
                for ioa in self.pending_ioas:
                    if ioa in self.status_vars:
                        self.status_vars[ioa].set("Rejected")
                self.pending_ioas.clear()
            if reason:
                self._log(f"RTU rejected request: {reason}")
        self._enable_send_buttons()

    def _set_connection_state(self, text: str) -> None:
        self.connection_var.set(text)
        self.connect_button.configure(text="Disconnect" if self.connected else "Connect",
                                      state="normal")
        self._enable_send_buttons()

    def _enable_send_buttons(self) -> None:
        for ioa, button in self.send_buttons.items():
            enabled = self.connected and (ioa not in self.pending_ioas)
            button.configure(state="normal" if enabled else "disabled")

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _close(self) -> None:
        self.closing = True
        self.client.disconnect()
        self.destroy()


if __name__ == "__main__":
    PointSimulatorApp().mainloop()
