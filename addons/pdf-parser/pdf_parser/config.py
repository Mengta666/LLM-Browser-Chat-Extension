import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    vlm_url: str
    vlm_model: str
    vlm_key: str = field(repr=False)
    layout_dir: str
    device: str = "gpu:0"
    vlm_concurrency: int = 2
    max_bytes: int = 8 * 1024 * 1024
    max_pixels: int = 16_000_000
    max_result_bytes: int = 8 * 1024 * 1024
    page_timeout: float = 180
    startup_timeout: float = 180
    max_new_tokens: int = 4096
    image_ocr: bool = True

    @classmethod
    def from_env(cls):
        result = cls(
            api_key=os.getenv("PDF_PARSER_API_KEY", ""),
            vlm_url=os.getenv("OCR_VLLM_BASE_URL", "http://127.0.0.1:18083/v1").rstrip("/"),
            vlm_model=os.getenv("OCR_VLLM_MODEL", "PaddlePaddle/PaddleOCR-VL-1.6"),
            vlm_key=os.getenv("OCR_VLLM_API_KEY", ""),
            layout_dir=os.getenv("OCR_LAYOUT_MODEL_DIR", ""),
            device=os.getenv("OCR_LAYOUT_DEVICE", "gpu:0"),
            vlm_concurrency=int(os.getenv("OCR_VLM_CONCURRENCY", "2")),
            max_bytes=int(os.getenv("PDF_PARSER_MAX_BYTES", "8388608")),
            max_pixels=int(os.getenv("PDF_PARSER_MAX_PIXELS", "16000000")),
            page_timeout=float(os.getenv("PDF_PARSER_PAGE_TIMEOUT", "180")),
            startup_timeout=float(os.getenv("PDF_PARSER_STARTUP_TIMEOUT", "180")),
            max_new_tokens=int(os.getenv("OCR_MAX_NEW_TOKENS", "4096")),
            image_ocr=os.getenv('OCR_IMAGE_TEXT', 'true').lower() == 'true',
        )
        result.validate()
        return result

    def validate(self):
        for name, key in (("PDF_PARSER_API_KEY", self.api_key), ("OCR_VLLM_API_KEY", self.vlm_key)):
            if len(key) < 32 or not key.isascii() or any(c.isspace() for c in key):
                raise ValueError(f"{name} must contain at least 32 non-whitespace ASCII characters")
        url = urlsplit(self.vlm_url)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username
                or url.password or url.query or url.fragment or not url.path.endswith("/v1")):
            raise ValueError("OCR_VLLM_BASE_URL must be an HTTP(S) /v1 base URL without credentials")
        if not self.vlm_model or not self.layout_dir or not Path(self.layout_dir).is_dir():
            raise ValueError("Prepare OCR_LAYOUT_MODEL_DIR and set OCR_VLLM_MODEL before starting")
        if (not 1 <= self.vlm_concurrency <= 8 or not 1024 <= self.max_bytes <= 32 * 1024 * 1024
                or not 4096 <= self.max_pixels <= 40_000_000 or not 1 <= self.page_timeout <= 600
                or not 1 <= self.startup_timeout <= 600 or not 128 <= self.max_new_tokens <= 16384):
            raise ValueError("Invalid parser resource limits")
