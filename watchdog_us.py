"""
USHybrid AI -- watchdog_us.py  (Galahad)
Monitors main_ushybrid.py and automatically restarts on crash or freeze.
Max 5 restarts per hour. Heartbeat logged every 30 minutes.
Checks for restart.flag in logs/ folder.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

_ENV_PATH = BASE_DIR / ".env"
if _ENV_PATH.exists():
    load_dotenv(dotenv_path=_ENV_PATH)
else:
    _TIDE_ENV = BASE_DIR.parent / "TideTraderAI" / ".env"
    if _TIDE_ENV.exists():
        load_dotenv(dotenv_path=_TIDE_ENV)
MAIN_SCRIPT   = BASE_DIR / "main_ushybrid.py"
LOG_FILE      = BASE_DIR / "logs" / "watchdog.log"
RESTART_FLAG  = BASE_DIR / "logs" / "restart.flag"
SHUTDOWN_FLAG = BASE_DIR / "logs" / "shutdown.flag"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

FREEZE_TIMEOUT  = 900   # 15 min of no output before declaring a freeze (was 600 / 10 min)
CPU_HANG_SECS   = 600
OUTPUT_GUARD    = 300
CHECK_INTERVAL  = 30
HEARTBEAT_EVERY = 1800
RESTART_DELAY   = 20
MAX_RESTARTS    = 5
RESTART_WINDOW  = 3600

# ALBION STANDING RULE: all log timestamps are UTC (never BST/local). See main_ushybrid.py.
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("USHybrid.Galahad")

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
_PO_USER  = os.getenv("PUSHOVER_USER_KEY",  "")
_PO_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "")


def _push(title: str, message: str, priority: int = 0) -> None:
    if not _PO_USER or not _PO_TOKEN:
        return
    try:
        requests.post(
            _PUSHOVER_URL,
            data={"token": _PO_TOKEN, "user": _PO_USER,
                  "title": f"[US500] {title}", "message": message, "priority": priority},
            timeout=5,
        )
    except Exception:
        pass


class _OutputReader(threading.Thread):
    def __init__(self, proc: subprocess.Popen) -> None:
        super().__init__(daemon=True, name="OutputReader")
        self._proc          = proc
        self.last_output_at = time.monotonic()

    def run(self) -> None:
        try:
            for raw in iter(self._proc.stdout.readline, b""):
                self.last_output_at = time.monotonic()
                try:
                    text = raw.decode("utf-8", errors="replace").rstrip()
                    if text:
                        print(f"  {text}", flush=True)
                except Exception:
                    pass
        except Exception:
            pass


class Watchdog:

    def __init__(self) -> None:
        self._proc:           Optional[subprocess.Popen] = None
        self._reader:         Optional[_OutputReader]    = None
        self._restart_times:  list                       = []
        self._proc_start:     float                      = 0.0
        self._heartbeat_at:   float                      = 0.0
        self._cpu_zero_since: Optional[float]            = None
        self._stopped:        bool                       = False

    def _launch(self) -> None:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self._proc = subprocess.Popen(
            [sys.executable, "-u", str(MAIN_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR),
            env=env,
            bufsize=0,
            **kwargs,
        )
        self._reader         = _OutputReader(self._proc)
        self._reader.start()
        self._proc_start     = time.monotonic()
        self._heartbeat_at   = time.monotonic()
        self._cpu_zero_since = None
        log.info("USHybrid AI started (PID %d)", self._proc.pid)

    def _terminate(self, grace_secs: int = 15) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            self._proc = None
            return
        pid = self._proc.pid
        try:
            if sys.platform == "win32":
                os.kill(pid, signal.CTRL_C_EVENT)
            else:
                self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=grace_secs)
            except subprocess.TimeoutExpired:
                log.warning("Grace period expired -- force-killing PID %d", pid)
                self._proc.kill()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        except Exception as exc:
            log.warning("Error during shutdown (PID %d): %s -- forcing kill", pid, exc)
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:
                pass
        finally:
            self._proc = None

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _check_frozen(self) -> Optional[str]:
        if self._reader is None:
            return None
        silent = time.monotonic() - self._reader.last_output_at
        if silent >= FREEZE_TIMEOUT:
            return f"No output for {int(silent // 60)} minutes (frozen)"
        return None

    def _check_cpu_hung(self) -> Optional[str]:
        if not self._alive() or self._reader is None:
            return None
        silent = time.monotonic() - self._reader.last_output_at
        if silent < OUTPUT_GUARD:
            self._cpu_zero_since = None
            return None
        try:
            import psutil
            cpu = psutil.Process(self._proc.pid).cpu_percent(interval=1)
            if cpu < 0.5:
                if self._cpu_zero_since is None:
                    self._cpu_zero_since = time.monotonic()
                elif time.monotonic() - self._cpu_zero_since >= CPU_HANG_SECS:
                    zero_mins = int((time.monotonic() - self._cpu_zero_since) // 60)
                    return f"0% CPU for {zero_mins} minutes (hung)"
            else:
                self._cpu_zero_since = None
        except Exception:
            pass
        return None

    def _check_restart_flag(self) -> bool:
        """Check for restart.flag -- triggers a clean restart."""
        if RESTART_FLAG.exists():
            RESTART_FLAG.unlink(missing_ok=True)
            log.info("restart.flag detected -- restarting USHybrid AI")
            return True
        return False

    def _shutdown_requested(self) -> bool:
        """
        Detect the dashboard's shutdown.flag. This is a full-stack stop signal:
        the main engine exits on it and we (the watchdog) must NOT relaunch it.
        We own the flag's lifecycle -- consume it here and shut everything down.
        """
        return SHUTDOWN_FLAG.exists()

    def _prune_restarts(self) -> None:
        cutoff = time.monotonic() - RESTART_WINDOW
        self._restart_times = [t for t in self._restart_times if t > cutoff]

    def _at_limit(self) -> bool:
        self._prune_restarts()
        return len(self._restart_times) >= MAX_RESTARTS

    def _do_restart(self, reason: str) -> bool:
        self._prune_restarts()
        attempt = len(self._restart_times) + 1
        log.warning("-" * 60)
        log.warning("RESTART %d/%d -- %s", attempt, MAX_RESTARTS, reason)
        log.warning("-" * 60)
        _push(
            f"Restarting ({attempt}/{MAX_RESTARTS})",
            f"Crash/freeze: {reason}\nRestarting in {RESTART_DELAY}s.",
            priority=1,
        )
        self._terminate()
        log.info("Waiting %ds before restart...", RESTART_DELAY)
        time.sleep(RESTART_DELAY)
        try:
            self._launch()
        except Exception as exc:
            log.error("Launch failed: %s", exc)
            return False
        if self._alive():
            self._restart_times.append(time.monotonic())
            log.info("USHybrid AI restarted (PID %d)", self._proc.pid)
            _push("Restarted OK", f"System restarted after: {reason}")
            return True
        log.error("Restart failed -- process not alive")
        return False

    def _handle_limit_exceeded(self) -> None:
        self._prune_restarts()
        msg = (
            f"USHybrid AI crashed {MAX_RESTARTS}x in 1 hour. "
            "Galahad stopped. Manual intervention required."
        )
        log.error("=" * 60)
        log.error("MAX RESTARTS EXCEEDED -- GALAHAD STOPPING")
        log.error(msg)
        log.error("=" * 60)
        _push("URGENT: Manual Intervention Required", msg, priority=1)
        self._terminate()

    def _maybe_heartbeat(self) -> None:
        if time.monotonic() - self._heartbeat_at < HEARTBEAT_EVERY:
            return
        uptime_h = (time.monotonic() - self._proc_start) / 3600
        log.info(
            "Galahad heartbeat -- USHybrid AI running for %.1f hours",
            uptime_h,
        )
        self._heartbeat_at = time.monotonic()

    def run(self) -> None:
        log.info("=" * 60)
        log.info("  USHybrid AI -- Galahad Watchdog")
        log.info("  Monitoring:  %s", MAIN_SCRIPT.name)
        log.info("  Max restarts: %d per hour", MAX_RESTARTS)
        log.info("  Heartbeat:   every %d min", HEARTBEAT_EVERY // 60)
        log.info("  Log:         %s", LOG_FILE)
        log.info("=" * 60)
        try:
            self._launch()
        except Exception as exc:
            log.error("FATAL: Could not start USHybrid AI: %s", exc)
            return
        while not self._stopped:
            try:
                time.sleep(CHECK_INTERVAL)
                if self._stopped:
                    break

                # Full-stack shutdown requested (dashboard button). Stop the
                # engine and the watchdog itself -- do NOT relaunch.
                if self._shutdown_requested():
                    log.info("shutdown.flag detected -- clean full shutdown, not restarting")
                    self.shutdown()
                    SHUTDOWN_FLAG.unlink(missing_ok=True)
                    return

                if self._check_restart_flag():
                    self._do_restart("restart.flag triggered")
                    continue

                if not self._alive():
                    # If the engine exited because of a shutdown request, honour
                    # it -- don't treat a clean, intentional exit as a crash.
                    if self._shutdown_requested():
                        log.info("Engine exited under shutdown.flag -- clean shutdown, not restarting")
                        self.shutdown()
                        SHUTDOWN_FLAG.unlink(missing_ok=True)
                        return
                    code   = self._proc.returncode if self._proc else "unknown"
                    reason = f"Process exited (code {code})"
                    log.warning(reason)
                    if self._at_limit():
                        self._handle_limit_exceeded()
                        return
                    self._do_restart(reason)
                    continue

                freeze_reason = self._check_frozen()
                if freeze_reason:
                    log.warning(freeze_reason)
                    if self._at_limit():
                        self._handle_limit_exceeded()
                        return
                    self._do_restart(freeze_reason)
                    continue

                cpu_reason = self._check_cpu_hung()
                if cpu_reason:
                    log.warning(cpu_reason)
                    if self._at_limit():
                        self._handle_limit_exceeded()
                        return
                    self._do_restart(cpu_reason)
                    continue

                self._maybe_heartbeat()

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.error("Watchdog loop error (continuing): %s", exc)

    def shutdown(self) -> None:
        log.info("")
        log.info("=" * 60)
        log.info("  Galahad -- Shutdown requested")
        log.info("=" * 60)
        self._stopped = True
        self._terminate(grace_secs=20)
        _push("Shutdown", "USHybrid AI stopped cleanly.")
        log.info("Galahad stopped cleanly.")


if __name__ == "__main__":
    watchdog = Watchdog()
    try:
        watchdog.run()
    except KeyboardInterrupt:
        watchdog.shutdown()
    sys.exit(0)
