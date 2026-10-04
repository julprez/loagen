"""Procesos locales: drenar sin acumular salida ilimitada."""
import os
import signal
import subprocess
import threading
import time


def shell_command(command: str, shell: str = '') -> list[str]:
    if shell:
        base = os.path.basename(shell).lower()
        if base in ('cmd', 'cmd.exe'):
            return [shell, '/d', '/s', '/c', command]
        if base in ('powershell', 'powershell.exe', 'pwsh', 'pwsh.exe'):
            return [shell, '-NoProfile', '-NonInteractive', '-Command', command]
        return [shell, '-c', command]
    if os.name == 'nt':
        return [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/s', '/c', command]
    return ['/bin/sh', '-c', command]


def terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == 'posix':
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        # stdlib cannot guarantee killing every detached descendant on Windows.
        proc.kill()
    proc.wait(timeout=5)


def run_bounded(argv: list[str], cwd: str, timeout: float, max_bytes: int,
                env: dict | None = None) -> tuple[int, str, bool]:
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=os.name == 'posix')
    assert proc.stdout is not None
    pipe = proc.stdout
    output = bytearray()
    truncated = threading.Event()

    def drain():
        try:
            while True:
                data = pipe.read(4096)
                if not data:
                    break
                room = max(0, max_bytes - len(output))
                output.extend(data[:room])
                if len(data) > room:
                    truncated.set()
        except (OSError, ValueError):
            # The pipe is closed by cleanup after termination.
            return

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if truncated.is_set():
                terminate(proc)
                break
            if time.monotonic() >= deadline:
                terminate(proc)
                raise subprocess.TimeoutExpired(argv, timeout)
            time.sleep(.01)
        thread.join(timeout=max(.01, deadline - time.monotonic()))
        if thread.is_alive():
            raise subprocess.TimeoutExpired(argv, timeout)
        return proc.returncode, output.decode('utf-8', 'replace'), truncated.is_set()
    finally:
        terminate(proc)
        pipe.close()
        thread.join(timeout=1)
