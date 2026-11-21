"""Install the pinned, quantized MIT-licensed OpenAI CLIP image encoder at build time."""
import hashlib
import os
from pathlib import Path
from urllib.request import urlopen

URL = (
    "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/"
    "6b7c523703ae26e39be4b03774966f8571c7f523/onnx/vision_model_quantized.onnx"
)
SHA256 = "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299"
SIZE = 89117001
TARGET = Path("/opt/msj-style/vision.onnx")


def install():
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temp = TARGET.with_suffix(".part")
    for attempt in range(3):
        try:
            digest = hashlib.sha256()
            with urlopen(URL, timeout=120) as response, temp.open("wb") as out:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    out.write(chunk)
                    digest.update(chunk)
                    if out.tell() > SIZE:
                        raise ValueError("Unexpected encoder size")
            if temp.stat().st_size != SIZE or digest.hexdigest() != SHA256:
                raise ValueError("Invalid encoder checksum")
            temp.chmod(0o444)
            os.replace(temp, TARGET)
            return
        except (OSError, ValueError):
            temp.unlink(missing_ok=True)
            if attempt == 2:
                raise


if __name__ == "__main__":
    install()
