"""Count and bound model spend for the original group's approved 27 November trial."""

import base64
import binascii
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime

from PIL import Image

from trial_shared_hour import eligible_service

MODEL = "mistral-small-2603"
APPROVED_START = int(datetime(2026, 11, 27, 20, 1, 10, tzinfo=UTC).timestamp())
MAX_CALLS = 100
# Millionths of one dollar. Upper bound applies even if no provider receipt is received.
MAX_COST = 500_000
COST_PER_CALL = 3_600       # 20k input at $0.15/M and 1k output at $0.60/M
COST_PER_LEGACY = 40_000    # Full 256K model context as input plus maximum legacy output
log = logging.getLogger(__name__)


def _bounded(payload):
    """Count text by encoded byte and reserve 4,096 input tokens per constrained image."""
    if not isinstance(payload, dict) or set(payload) - {
        "messages", "model", "max_tokens", "temperature", "top_p", "response_format", "reasoning_effort"
    }:
        return False
    if payload.get("model") != MODEL or type(payload.get("max_tokens")) is not int:
        return False
    messages = payload.get("messages")
    if not 1 <= payload["max_tokens"] <= 1000 or not isinstance(messages, list) or not 1 <= len(messages) <= 3:
        return False
    # Bounds also include structured-output schema and prompt/control token overhead.
    try:
        text_bytes = 512 + len(json.dumps({k:v for k,v in payload.items() if k != "messages"},
                                           ensure_ascii=False).encode())
        images = 0
        for msg in messages:
            if not isinstance(msg, dict) or set(msg) != {"role","content"} or msg["role"] not in ("system","user"):
                return False
            content = msg["content"]
            if isinstance(content, str):
                text_bytes += len(content.encode()) + 12
                continue
            if not isinstance(content, list) or not 1 <= len(content) <= 8:
                return False
            for part in content:
                if not isinstance(part, dict):
                    return False
                if set(part) == {"type", "text"} and part["type"] == "text" and isinstance(part["text"], str):
                    text_bytes += len(part["text"].encode()) + 12
                    continue
                if set(part) != {"type","image_url"} or part["type"] != "image_url":
                    return False
                url = part["image_url"]
                if isinstance(url, dict) and set(url) == {"url"}:
                    url = url["url"]
                if not isinstance(url,str) or len(url)>500_000 or not url.startswith("data:image/jpeg;base64,"):
                    return False
                images += 1
                if images > 3:
                    return False
                raw = base64.b64decode(url.partition(",")[2],validate=True)
                with Image.open(io.BytesIO(raw)) as im:
                    if im.format != "JPEG" or im.mode != "RGB" or max(im.size)>512 or min(im.size)<1:
                        return False
        # The pinned processor uses 14px patches with a 2x spatial merge. Even
        # at its own maximum 1540px edge, a square image is under 4,096 tokens.
        return text_bytes + images*4096 <= 20_000
    except (ValueError, TypeError, OverflowError, OSError, binascii.Error, Image.DecompressionBombError):
        return False


def _window_open(conn):
    window = conn.execute("SELECT started,ends FROM hour_window WHERE id=1").fetchone()
    return (window and int(window[0]) == APPROVED_START and abs(window[1] - window[0] - 3600) < .01
            and window[0] <= time.time() < window[1])


def _usage_state(conn, has_meter=True):
    used_row = conn.execute("SELECT used FROM hour_counters WHERE kind='model'").fetchone()
    used = used_row[0] if used_row else 0
    tracked = conn.execute("SELECT COUNT(*) FROM hour_model_meter").fetchone()[0] if has_meter else 0
    legacy = used - tracked
    if not 0 <= legacy <= 3 or (legacy and os.getenv("FINDER_TRIAL_HOUR_MODEL", MODEL) != MODEL):
        return None
    return used, tracked*COST_PER_CALL + legacy*COST_PER_LEGACY


def _allowed(conn):
    if not _window_open(conn):
        return False
    has_meter = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hour_model_meter'").fetchone())
    state = _usage_state(conn, has_meter)
    if not state or state[0] >= MAX_CALLS or state[1]+COST_PER_CALL > MAX_COST:
        return False
    return not has_meter or not conn.execute("""SELECT 1 FROM hour_model_meter WHERE
        prompt>20000 OR completion>1000 OR reasoning>1000 OR total>21000 OR total-prompt>1000 LIMIT 1""").fetchone()


def available(path):
    if not eligible_service():
        return False
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",uri=True,timeout=.2)) as conn:
            return _allowed(conn)
    except (sqlite3.Error,OSError):
        return False


def reserve(path, payload):
    """Reserve before sending. Errors and ambiguous requests keep their entire charge."""
    if not eligible_service() or not _bounded(payload):
        return None
    try:
        with closing(sqlite3.connect(path,timeout=.5)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if not _allowed(conn):
                return None
            conn.execute("""CREATE TABLE IF NOT EXISTS hour_model_meter(
                ticket INTEGER PRIMARY KEY,prompt INTEGER,completion INTEGER,reasoning INTEGER,total INTEGER)""")
            conn.execute("INSERT INTO hour_counters VALUES('model',1) ON CONFLICT(kind) DO UPDATE SET used=used+1")
            ticket = conn.execute("SELECT used FROM hour_counters WHERE kind='model'").fetchone()[0]
            conn.execute("INSERT INTO hour_model_meter(ticket) VALUES(?)",(ticket,))
            return ticket
    except (sqlite3.Error,OSError):
        return None


def record_usage(path, ticket, usage):
    if not isinstance(usage,dict) or type(ticket) is not int:
        return
    details = usage.get("completion_tokens_details") or {}
    if not isinstance(details,dict):
        return
    values = (usage.get("prompt_tokens"),usage.get("completion_tokens"),
              details.get("reasoning_tokens",usage.get("reasoning_tokens")),usage.get("total_tokens"))
    if any(v is not None and (type(v) is not int or not 0<=v<=10**8) for v in values):
        return
    if values[0] is None or values[1] is None:
        return
    try:
        with closing(sqlite3.connect(path,timeout=.5)) as conn, conn:
            if not conn.execute("""UPDATE hour_model_meter SET prompt=?,completion=?,reasoning=?,total=?
                 WHERE ticket=? AND prompt IS NULL""",(*values,ticket)).rowcount:
                return
            state = _usage_state(conn)
            if state is None:
                return
            used,limit = state
            receipts, ins, outs, reason = conn.execute("""SELECT count(prompt),sum(prompt),
                sum(max(completion,coalesce(total-prompt,0),coalesce(reasoning,0))),sum(reasoning)
                FROM hour_model_meter""").fetchone()
            log.info("Finder hourly model usage calls=%d receipts=%d input_tokens=%d output_tokens=%d "
                     "reasoning_tokens=%d reserved_micro_usd=%d",used,receipts,ins or 0,outs or 0,reason or 0,limit)
    except (sqlite3.Error,OSError):
        return
