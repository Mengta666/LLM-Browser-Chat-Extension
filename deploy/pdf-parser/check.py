import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sys
from urllib import error, request
from urllib.parse import urlsplit

from manage import DEPLOY, load_env


class CheckError(Exception):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(url, key, payload=None, timeout=10, content_type=None):
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
            or parts.password or parts.fragment):
        raise CheckError("invalid_url")
    headers = {"Authorization": "Bearer " + key}
    if content_type:
        headers["Content-Type"] = content_type
    req = request.Request(url, data=payload, headers=headers)
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=timeout) as response:
        content = response.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise CheckError("response_too_large")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise CheckError("invalid_response")
        return result


def page_image(image, pdf, page, dpi, max_bytes, max_pixels):
    if pdf:
        import pymupdf

        if not 72 <= dpi <= 300:
            raise CheckError("dpi_out_of_range")
        with pymupdf.open(pdf) as document:
            if not document.is_pdf or document.needs_pass or not 1 <= page <= len(document):
                raise CheckError("invalid_pdf_or_page")
            item = document[page - 1]
            estimated = math.ceil(item.rect.width * dpi / 72) * math.ceil(item.rect.height * dpi / 72)
            if estimated > max_pixels:
                raise CheckError("page_too_large_reduce_dpi")
            pixmap = item.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
            content, mime, dimensions = pixmap.tobytes("png"), "image/png", (pixmap.width, pixmap.height)
    else:
        if image.stat().st_size > max_bytes:
            raise CheckError("image_too_large")
        content = image.read_bytes()
        # PNG/JPEG 元数据校验只依赖客户端的小型 Pillow 包。
        from PIL import Image

        with Image.open(io.BytesIO(content)) as opened:
            if opened.format not in ("PNG", "JPEG") or getattr(opened, "n_frames", 1) != 1:
                raise CheckError("unsupported_image")
            if opened.getexif().get(274, 1) != 1:
                raise CheckError("image_orientation_not_normalized")
            dimensions = opened.size
            mime = "image/png" if opened.format == "PNG" else "image/jpeg"
    if len(content) > max_bytes or dimensions[0] * dimensions[1] > max_pixels:
        raise CheckError("image_too_large")
    return content, mime, dimensions


def validate_page(result, content, dimensions, page, expected):
    if (result.get("status") != "ok" or result.get("service") != "pdf-parser"
            or result.get("schema_version") != 1 or result.get("page_number") != page
            or (result.get("width"), result.get("height")) != dimensions
            or result.get("coordinate_space") != "input_image_pixels"
            or result.get("image_sha256") != hashlib.sha256(content).hexdigest()):
        raise CheckError("invalid_parse_response")
    blocks, text = result.get("blocks"), result.get("text")
    if not isinstance(blocks, list) or not blocks or not isinstance(text, str) or not text.strip():
        raise CheckError("no_content_recognized")
    for block in blocks:
        box = block.get("bbox") if isinstance(block, dict) else None
        if (not isinstance(box, list) or len(box) != 4
                or not all(type(v) in (int, float) and math.isfinite(v) for v in box)
                or not (0 <= box[0] <= box[2] <= dimensions[0] and 0 <= box[1] <= box[3] <= dimensions[1])):
            raise CheckError("invalid_block_bbox")
    if result.get("truncated") is True:
        raise CheckError("ocr_output_truncated")
    matches = [phrase.casefold() in text.casefold() for phrase in expected]
    if not all(matches):
        raise CheckError("expected_text_missing")
    return {"check": "page_inference", "ok": True, "page": page, "blocks": len(blocks),
            "content_length": len(text), "expected_checks": len(matches),
            "truncated": result.get("truncated"), "elapsed_ms": result.get("elapsed_ms"),
            "warnings": result.get("warnings", [])}


def main():
    parser = argparse.ArgumentParser(description="Explicit single-page OCR smoke test; no KB writes")
    parser.add_argument("--env-file", type=Path, default=DEPLOY / ".env")
    parser.add_argument("--from-environment", action="store_true", help="Read container environment, not .env")
    parser.add_argument("--parser-url", default="http://127.0.0.1:19082")
    parser.add_argument("--vlm-url", help="Optional direct vLLM /v1 base URL")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--image", type=Path)
    inputs.add_argument("--pdf", type=Path)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--expect", action="append", default=[], help="Known text; may repeat")
    parser.add_argument("--save-result", type=Path, help="Explicit private JSON artifact; must not already exist")
    args = parser.parse_args()
    reports = []
    try:
        if args.page < 1 or (args.save_result and args.save_result.exists()):
            raise CheckError("invalid_page_or_output_already_exists")
        if (args.expect or args.save_result) and not (args.image or args.pdf):
            raise CheckError("image_or_pdf_required")
        values = dict(os.environ) if args.from_environment else load_env(args.env_file.resolve())
        if args.vlm_url:
            result = fetch_json(args.vlm_url.rstrip("/") + "/models", values["OCR_VLLM_API_KEY"])
            if not any(m.get("id") == values["OCR_VLLM_MODEL"] for m in result.get("data", [])
                       if isinstance(m, dict)):
                raise CheckError("vlm_model_not_found")
            reports.append({"check": "vlm_models", "ok": True, "inference_verified": False})
        base = args.parser_url.rstrip("/")
        ready = fetch_json(base + "/ready", values["PDF_PARSER_API_KEY"])
        if (ready.get("status") != "ready" or ready.get("schema_version") != 1
                or ready.get("vlm_model") != values["OCR_VLLM_MODEL"]):
            raise CheckError("parser_not_ready")
        reports.append({"check": "parser_ready", "ok": True, "inference_verified": False})
        if args.image or args.pdf:
            content, mime, dimensions = page_image(args.image, args.pdf, args.page, args.dpi,
                                                  int(values["PDF_PARSER_MAX_BYTES"]),
                                                  int(values["PDF_PARSER_MAX_PIXELS"]))
            result = fetch_json(base + f"/v1/parse/page?page_number={args.page}", values["PDF_PARSER_API_KEY"],
                                content, float(values["PDF_PARSER_PAGE_TIMEOUT"]) + 15, mime)
            reports.append(validate_page(result, content, dimensions, args.page, args.expect))
            if args.save_result:
                descriptor = os.open(args.save_result, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
    except Exception as exc:
        failure = {"check": "failed", "ok": False, "error_type": type(exc).__name__}
        if isinstance(exc, CheckError):
            failure["error_code"] = str(exc)
        if isinstance(exc, error.HTTPError):
            failure["http_status"] = exc.code
        reports.append(failure)
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0 if reports and all(item["ok"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
