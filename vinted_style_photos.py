"""Compare one public Home cover to the owner's calibrated visual style profiles."""
import base64
from functools import lru_cache
import io
import json
import os
from pathlib import Path
import subprocess
import sys

PROFILES = Path(__file__).with_name("style_assets") / "home_photo_profiles.json"
MODEL = Path(os.environ.get("MSJ_HOME_STYLE_MODEL", "/opt/msj-style/vision.onnx"))
MAX_BYTES = 320 * 1024


@lru_cache(maxsize=1)
def profiles():
    return json.loads(PROFILES.read_text(encoding="utf-8"))


def photo_matches(url, eligible):
    """Return (styles, checked). Keep the encoder out of the long-lived worker."""
    from alert_images import download_photo, safe_listing_photo

    keys = sorted(set(eligible).intersection(profiles()))
    if not keys or not safe_listing_photo(url) or not MODEL.is_file():
        return (), False
    cover = download_photo(url, max_bytes=MAX_BYTES, max_seconds=2.5)
    if not cover:
        return (), False
    env = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    try:
        result = subprocess.run(
            [sys.executable, __file__, *keys], input=cover, capture_output=True,
            timeout=5, env=env, check=False,
        )
        if result.returncode:
            return (), False
        found = json.loads(result.stdout)
        if not isinstance(found, list) or len(found) > 1 or any(k not in keys for k in found):
            return (), False
        return tuple(found), True
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return (), False


def rank(embedding, eligible):
    import numpy as np

    norm = np.linalg.norm(embedding)
    if not np.isfinite(norm) or norm <= 0:
        return []
    embedding = embedding / norm
    passed = []
    for key in sorted(set(eligible).intersection(profiles())):
        profile = profiles()[key]

        def similarity(field):
            rows = np.frombuffer(base64.b64decode(profile[field]), np.int8).reshape(-1, 512)
            unit_rows = rows.astype(np.float32)
            unit_rows /= np.linalg.norm(unit_rows, axis=1, keepdims=True)
            return float((unit_rows @ embedding).max())

        margin = similarity("positive") - similarity("negative") - profile["min_margin"]
        closeness = similarity("examples") - profile["min_similarity"]
        if margin >= 0 and closeness >= 0:
            passed.append((margin + .3 * closeness, key))
    return [max(passed)[1]] if passed else []


def evaluate(cover, eligible):
    import numpy as np
    import onnxruntime as ort
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(cover)) as image:
        if image.width * image.height > 12_000_000:
            raise ValueError("Oversize cover")
        image = ImageOps.contain(
            ImageOps.exif_transpose(image).convert("RGB"), (224, 224), Image.Resampling.BICUBIC,
        )
        canvas = Image.new("RGB", (224, 224), (242, 242, 242))
        canvas.paste(image, ((224 - image.width) // 2, (224 - image.height) // 2))
    pixels = np.asarray(canvas, dtype=np.float32) / 255
    mean = np.array([.48145466, .4578275, .40821073], np.float32)
    std = np.array([.26862954, .26130258, .27577711], np.float32)
    batch = np.ascontiguousarray(((pixels - mean) / std).transpose(2, 0, 1)[None])
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.enable_cpu_mem_arena = False
    session = ort.InferenceSession(str(MODEL), options, providers=["CPUExecutionProvider"])
    embedding = session.run(["image_embeds"], {"pixel_values": batch})[0].reshape(512)
    return rank(embedding, eligible)


if __name__ == "__main__":
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        sys.exit(1)
    try:
        print(json.dumps(evaluate(data, sys.argv[1:])))
    except Exception:
        sys.exit(1)
