"""Restart a stopped local worker without duplicating a healthy process."""

import multiprocessing

from logger import get_logger

logger = get_logger(__name__)


def ensure_running(process, target, args=(), *, name):
    if process is not None and process.is_alive():
        return process
    if process is not None:
        process.join(timeout=0)
        logger.warning("Restarting stopped %s worker; exit=%s", name, process.exitcode)
    replacement = multiprocessing.Process(target=target, args=args, name=name)
    replacement.start()
    return replacement
