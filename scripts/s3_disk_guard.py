#!/usr/bin/env python3
"""Run an S3 build/import in its own process group, with a disk reserve."""

import argparse
import os
import shutil
import signal
import subprocess
import time

GIB = 1024 ** 3


def stop_process_group(process):
    # Kill descendants even if the group leader has already exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run(command, path, reserve_gib, interval=2, repeat=0):
    interrupted = [False]

    def stop(signum, frame):
        interrupted[0] = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    process = None
    try:
        next_start = time.monotonic() + repeat
        while True:
            if interrupted[0]:
                return 143
            free = shutil.disk_usage(path).free
            if free < reserve_gib * GIB:
                print(f"Disk reserve reached: {free / GIB:.2f} GiB free; "
                      f"reserve={reserve_gib} GiB. Stopping command group.", flush=True)
                return 75
            if process is None and time.monotonic() >= next_start:
                process = subprocess.Popen(command, start_new_session=True)
            if process is not None and process.poll() is not None:
                code = process.returncode
                stop_process_group(process)
                process = None
                if not repeat:
                    return code if code >= 0 else 128 - code
                print(f"Periodic import exited with status {code}", flush=True)
                next_start = time.monotonic() + repeat
            time.sleep(interval)
    finally:
        if process is not None:
            stop_process_group(process)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    parser.add_argument("--reserve-gib", type=float, required=True)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--repeat", type=float, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.reserve_gib <= 0 or args.interval <= 0 or args.repeat < 0:
        parser.error("command, positive reserve/interval, nonnegative repeat required")
    return run(command, args.path, args.reserve_gib, args.interval, args.repeat)


if __name__ == "__main__":
    raise SystemExit(main())
