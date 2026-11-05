"""Fresh-process entry point; reloads prompts on every run."""

import os
import sys
import time
from pathlib import Path

from .application import Application
from .backends import BackendRegistry
from .extraction_tasks import Tasks
from .storage import Store, writer_lock


def main():
    store = Store(Path(sys.argv[1]))
    task = sys.argv[2]
    tasks = Tasks(store.root)
    # The launcher briefly holds the writer lock while creating this process.
    for _attempt in range(50):
        try:
            with writer_lock(store.root):
                store.initialize()
                value = tasks.update(task, pid=os.getpid())
                app = Application(
                    store,
                    backends=BackendRegistry(
                        Path(value["config"]) if value.get("config") else None, value.get("backend")
                    ),
                )
                app.library._requested_task = task
                app.library._resume_index = value.get("resume_index")
                app.library.index(
                    value["book"], section_id=value["section"], new=value["new"], content_only=True
                )
                return
        except Exception as exc:
            if "另一写进程" in str(exc):
                time.sleep(0.1)
                continue
            tasks.update(task, status="failed", error=str(exc))
            return
    tasks.update(task, status="failed", error="数据目录正在使用，请稍后重试。")


if __name__ == "__main__":
    main()
