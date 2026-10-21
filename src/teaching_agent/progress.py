"""Plain-text CLI progress with a periodic elapsed-time heartbeat."""

import sys
import threading
import time


class DraftProgress:
    def __init__(self, interval=15, stream=None, lazy=False):
        self._lazy = lazy
        self._active = False
        self.interval = interval
        self.stream = stream if stream is not None else sys.stderr
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._message = "准备执行"
        self._started = time.monotonic()
        self._phase_started = self._started
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)

    def __enter__(self):
        if not self._lazy:
            self._start()
        return self

    def _start(self):
        self._started = self._phase_started = time.monotonic()
        self._active = True
        self._thread.start()

    def __exit__(self, *exc):
        if not self._active:
            return
        self._stop.set()
        self._thread.join()
        print(
            f"[进度] {'执行暂停' if exc[0] else '本次执行结束'}；"
            f"总耗时 {time.monotonic() - self._started:.0f} 秒",
            file=self.stream,
            flush=True,
        )

    def __call__(self, message):
        with self._lock:
            if not self._active:
                self._start()
            self._message = message
            self._phase_started = time.monotonic()
            print(f"[进度] {message}", file=self.stream, flush=True)

    def _heartbeat(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                elapsed = time.monotonic()
                print(
                    f"[等待] {self._message}；本阶段已等待 "
                    f"{elapsed - self._phase_started:.0f} 秒，"
                    f"总耗时 {elapsed - self._started:.0f} 秒（仍在等待当前阶段完成）",
                    file=self.stream,
                    flush=True,
                )
