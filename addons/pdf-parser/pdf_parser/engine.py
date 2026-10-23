import hashlib
import importlib.metadata
import io
import json
import math
import warnings

import httpx
from PIL import Image, UnidentifiedImageError

from . import SCHEMA_VERSION, VERSION
from .vlm_result import VLMResultTracker


class ParseError(Exception):
    def __init__(self, code):
        """保存可对外公开的解析错误码，不携带可能泄露配置的底层异常内容。"""
        self.code = code
        super().__init__(code)


def check_vlm(settings):
    """检查视觉模型接口及配置模型是否可用，不执行页面推理。"""
    try:
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            response = client.get(settings.vlm_url + "/models", headers={
                "Authorization": "Bearer " + settings.vlm_key,
            })
            response.raise_for_status()
            models = response.json().get("data", [])
            if not any(m.get("id") == settings.vlm_model for m in models if isinstance(m, dict)):
                raise ParseError("vlm_model_not_found")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        raise ParseError("vlm_unavailable") from exc


def decode_image(content, max_pixels):
    """验证图像尺寸和方向，解码为解析管线需要的 RGB 图像。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as opened:
                if opened.format not in ("PNG", "JPEG") or getattr(opened, "n_frames", 1) != 1:
                    raise ParseError("unsupported_image")
                if opened.width * opened.height > max_pixels:
                    raise ParseError("image_too_large")
                if opened.getexif().get(274, 1) != 1:
                    raise ParseError("image_orientation_not_normalized")
                opened.load()
                if "A" in opened.getbands() or "transparency" in opened.info:
                    opened = opened.convert("RGBA")
                    canvas = Image.new("RGB", opened.size, "white")
                    canvas.paste(opened, mask=opened.getchannel("A"))
                    return canvas
                return opened.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ParseError("invalid_image") from exc
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ParseError("image_too_large") from exc


def normalize_result(raw, width, height, page_number, image_sha256, versions):
    """校验并转换 OCR 输出，返回绑定页面、输入图像摘要和版本的结构化结果。"""
    page = raw.get("res") if isinstance(raw, dict) else None
    if not isinstance(page, dict) or not isinstance(page.get("parsing_res_list"), list):
        raise ParseError("invalid_pipeline_result")
    if page.get("width") != width or page.get("height") != height:
        raise ParseError("coordinate_space_mismatch")
    blocks, seen = [], set()
    for ordinal, source in enumerate(page["parsing_res_list"]):
        if not isinstance(source, dict):
            raise ParseError("invalid_pipeline_result")
        bbox = source.get("block_bbox")
        block_id = source.get("block_id")
        text = source.get("block_content")
        label = source.get("block_label")
        if (type(block_id) is not int or block_id in seen or not isinstance(text, str)
                or not isinstance(label, str) or not isinstance(bbox, list) or len(bbox) != 4
                or not all(type(v) in (int, float) and math.isfinite(v) for v in bbox)):
            raise ParseError("invalid_pipeline_result")
        if not (0 <= bbox[0] <= bbox[2] <= width and 0 <= bbox[1] <= bbox[3] <= height):
            raise ParseError("invalid_block_bbox")
        seen.add(block_id)
        blocks.append({
            "local_id": block_id, "ordinal": ordinal, "type": label, "text": text,
            "bbox": bbox, "reading_order": source.get("block_order"),
            "group_id": source.get("group_id"),
            "polygon": source.get("block_polygon_points"),
        })
    return {
        "status": "ok", "service": "pdf-parser", "version": VERSION,
        "schema_version": SCHEMA_VERSION, "page_number": page_number,
        "image_sha256": image_sha256, "width": width, "height": height,
        "coordinate_space": "input_image_pixels", "rotation_applied": 0,
        "pipeline": "PaddleOCR-VL-1.6", "layout_model": "PP-DocLayoutV3",
        "versions": versions, "blocks": blocks,
        "text": "\n\n".join(block["text"] for block in blocks if block["text"]),
        "raw_result": raw,
        "warnings": ["ocr_not_verified", "upstream_finish_reason_not_exposed"]
                    + (["no_blocks_detected"] if not blocks else []),
        "truncated": None,
    }


class PaddleEngine:
    def __init__(self, settings):
        """检查上游模型并初始化 OCR 管线、版本记录和结束原因跟踪器。"""
        from paddleocr import PaddleOCRVL

        check_vlm(settings)
        self.settings = settings
        self.versions = {name: importlib.metadata.version(name)
                         for name in ("paddleocr", "paddlex")}
        self.pipeline = PaddleOCRVL(
            pipeline_version="v1.6", layout_detection_model_name="PP-DocLayoutV3",
            layout_detection_model_dir=settings.layout_dir, device=settings.device,
            vl_rec_backend="vllm-server", vl_rec_server_url=settings.vlm_url,
            vl_rec_api_model_name=settings.vlm_model, vl_rec_api_key=settings.vlm_key,
            vl_rec_max_concurrency=settings.vlm_concurrency,
            use_doc_orientation_classify=False, use_doc_unwarping=False,
            use_layout_detection=True, use_chart_recognition=False,
            use_seal_recognition=False, use_ocr_for_image_block=settings.image_ocr,
            format_block_content=False, merge_layout_blocks=True,
            markdown_ignore_labels=[], use_queues=False,
        )
        self.tracker = VLMResultTracker(self.pipeline.paddlex_pipeline.vl_rec_model, self.versions)

    def parse(self, content, page_number):
        """执行一次图像版面解析，汇总块内容、模型结束原因及结果大小检查。"""
        import numpy as np

        picture = decode_image(content, self.settings.max_pixels)
        width, height = picture.size
        # ndarray 输入是 OpenCV BGR；关闭几何预处理以保持上传图像坐标。
        pixels = np.asarray(picture)[:, :, ::-1].copy()
        self.tracker.reset()
        results = list(self.pipeline.predict(
            pixels, max_new_tokens=self.settings.max_new_tokens, temperature=0.0,
        ))
        if len(results) != 1:
            raise ParseError("unexpected_page_count")
        raw = results[0].json
        result = normalize_result(raw, width, height, page_number,
                                  hashlib.sha256(content).hexdigest(), self.versions)
        generation = self.tracker.summary()
        result['generation'] = generation
        result['truncated'] = generation['truncated']
        result['versions'].update(parser=VERSION, image_ocr=self.settings.image_ocr, finish_adapter=1)
        if generation['truncated'] is not None:
            result['warnings'].remove('upstream_finish_reason_not_exposed')
        if generation['truncated']:
            result['warnings'].append('vlm_output_length_limit')
        if any(b['type'] == 'image' for b in result['blocks']):
            result['warnings'].append('image_text_does_not_establish_arrow_or_spatial_relations')
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > self.settings.max_result_bytes:
            raise ParseError("result_too_large")
        return result
