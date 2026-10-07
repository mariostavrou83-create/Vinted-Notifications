"""Restart a stopped local worker without duplicating a healthy process."""

import multiprocessing

from logger import get_logger

logger = get_logger(__name__)


def stop_process(process, *, name, timeout=5):
    """Bound shutdown even when a plugin waits for an unfinished async job."""
    if process is None:
        return True
    if process.is_alive():
        process.terminate()
    process.join(timeout=timeout)
    if process.is_alive():
        logger.warning("Forcing stopped %s worker to exit", name)
        process.kill()
        process.join(timeout=2)
    stopped = not process.is_alive()
    if not stopped:
        logger.error("%s worker has not exited; retaining its process handle", name)
    return stopped


def ensure_running(process, target, args=(), *, name):
    if process is not None and process.is_alive():
        return process
    if process is not None:
        process.join(timeout=0)
        logger.warning("Restarting stopped %s worker; exit=%s", name, process.exitcode)
    replacement = multiprocessing.Process(target=target, args=args, name=name)
    replacement.start()
    return replacement
