"""Threaded TCP client for the point simulator line protocol."""

from __future__ import annotations

import queue
import socket
import threading


class RtuClient:
    def __init__(self, events: queue.Queue[tuple]) -> None:
        self._events = events
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None

    def connect(self, host: str, port: int) -> None:
        try:
            connection = socket.create_connection((host, port), timeout=5.0)
            connection.settimeout(0.5)
        except OSError as error:
            self._events.put(("connect_error", str(error)))
            return
        with self._lock:
            self._socket = connection
        self._events.put(("connected", host, port))
        self._reader = threading.Thread(target=self._read_loop, args=(connection,), daemon=True)
        self._reader.start()

    def send_line(self, line: str) -> None:
        data = line.encode("ascii")
        with self._lock:
            connection = self._socket
            if connection is None:
                raise OSError("not connected")
            connection.sendall(data)

    def disconnect(self) -> None:
        with self._lock:
            connection = self._socket
            self._socket = None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def _read_loop(self, connection: socket.socket) -> None:
        pending = bytearray()
        reason: str | None = None
        try:
            while True:
                try:
                    chunk = connection.recv(1024)
                except socket.timeout:
                    with self._lock:
                        if self._socket is not connection:
                            break
                    continue
                if not chunk:
                    break
                pending.extend(chunk)
                while b"\n" in pending:
                    raw_line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    self._events.put(("line", raw_line.rstrip(b"\r").decode("utf-8", "replace")))
                if len(pending) > 1024:
                    reason = "response line exceeds 1024 bytes"
                    break
        except OSError as error:
            reason = str(error)
        finally:
            with self._lock:
                was_active = self._socket is connection
                if was_active:
                    self._socket = None
            try:
                connection.close()
            except OSError:
                pass
            if was_active:
                self._events.put(("disconnected", reason))
