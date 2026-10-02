"""Small, uncropped square collages for listing and reference photos."""

import asyncio
import io
import time
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps

SIZE = 1280


def cells(count, size=SIZE):
    if count == 1:
        return [(0, 0, size, size)]
    half = size // 2
    if count == 2:
        return [(0, 0, half, size), (half, 0, size, size)]
    if count == 3:
        return [(0, 0, half, size), (half, 0, size, half), (half, half, size, size)]
    if count == 4:
        return [
            (0, 0, half, half),
            (half, 0, size, half),
            (0, half, half, size),
            (half, half, size, size),
        ]
    raise ValueError("Choose between one and four photos.")


def collage(images):
    """Use normalized JPEG bytes; contain every image so labels are not cropped."""
    boxes = cells(len(images))
    canvas = Image.new("RGB", (SIZE, SIZE), "white")
    for raw, (left, top, right, bottom) in zip(images, boxes):
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.contain(
                image.convert("RGB"), (right - left - 12, bottom - top - 12)
            )
            canvas.paste(
                image,
                (
                    left + (right - left - image.width) // 2,
                    top + (bottom - top - image.height) // 2,
                ),
            )
    result = io.BytesIO()
    canvas.save(result, "JPEG", quality=90, optimize=True)
    return result.getvalue()


def reference_collage(streams):
    from dashboard_store import normalize_photo

    cells(len(streams))  # reject oversized batches before reading any files
    return collage([normalize_photo(stream) for stream in streams])


def safe_listing_photo(url):
    if not isinstance(url, str) or len(url) > 4096:
        return None
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        allowed = (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and (
                host == "vinted.net"
                or host.endswith(".vinted.net")
                or host == "i.ebayimg.com"
            )
        )
        return url if allowed else None
    except ValueError:
        return None


def photo_urls(item):
    """Keep the main photo first; never substitute thumbnails of it as extra photos."""
    raw = getattr(item, "raw_data", {}) or {}
    candidates = [getattr(item, "photo", None)]
    photos = raw.get("photos")
    for photo in (photos if isinstance(photos, list) else [])[:20]:
        candidates.append(photo.get("url") if isinstance(photo, dict) else photo)
    return list(dict.fromkeys(url for url in candidates if safe_listing_photo(url)))[:4]


def download_photo(url):
    """Only approved public listing image CDNs; bounded reads, no redirects."""
    if not safe_listing_photo(url):
        return None
    started = time.monotonic()
    try:
        with requests.get(
            url, stream=True, timeout=(2, 3), allow_redirects=False
        ) as response:
            if response.status_code != 200:
                return None
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 8 * 1024 * 1024 or time.monotonic() - started > 5:
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except requests.RequestException:
        return None


def normalize_available(images):
    from dashboard_store import normalize_photo

    valid = []
    for raw in images:
        if raw:
            try:
                valid.append(normalize_photo(io.BytesIO(raw)))
            except ValueError:
                pass
    return collage(valid) if valid else None


async def listing_collage(urls):
    images = await asyncio.gather(
        *(asyncio.to_thread(download_photo, url) for url in urls[:4])
    )
    result = await asyncio.to_thread(normalize_available, images)
    from logger import get_logger

    get_logger(__name__).info(
        "Listing collage downloaded %s/%s photos",
        sum(bool(raw) for raw in images),
        len(urls[:4]),
    )
    return result
