# PDF 页解析服务

部署入口：[Docker / WSL](../../deploy/pdf-parser/DOCKER.md)，另保留 [Linux 原生部署](../../deploy/pdf-parser/README.md)。不导入本项目后端，不保存知识库或文档任务。

职责：单页 PNG/JPEG → PP-DocLayoutV3 → PaddleOCR-VL-1.6 / vLLM → 原始 JSON＋规范化块。模型不直接读取 PDF 文件。

## HTTP 接口 v1

除 `/health` 外均要求 `Authorization: Bearer <PDF_PARSER_API_KEY>`。

| 接口 | 语义 |
| --- | --- |
| `GET /health` | 进程存活、版本、worker 是否存活；不推理 |
| `GET /ready` | worker 已加载、vLLM 模型名匹配；不推理 |
| `POST /v1/parse/page?page_number=1` | 请求体为原始 PNG/JPEG 字节，`Content-Type: image/png` 或 `image/jpeg` |

不接收 URL、服务器文件路径、多页 PDF 或任意模型/提示词。调用方负责 PDF 渲染和页号；页号从 1 开始。

成功返回的核心字段：

```json
{
  "status": "ok",
  "schema_version": 1,
  "page_number": 1,
  "image_sha256": "上传图像字节的 SHA256",
  "width": 1190,
  "height": 1684,
  "coordinate_space": "input_image_pixels",
  "rotation_applied": 0,
  "blocks": [{
    "local_id": 0,
    "ordinal": 0,
    "type": "text",
    "text": "识别内容",
    "bbox": [10, 20, 400, 90],
    "reading_order": 1,
    "group_id": 0,
    "polygon": null
  }],
  "text": "识别内容",
  "raw_result": {},
  "truncated": false,
  "generation": {"scope": "vlm_generation_only", "regions": [{"finish_reason": "stop", "completion_tokens": 8}], "truncated": false},
  "warnings": ["ocr_not_verified"]
}
```

此外返回 service/version、pipeline/layout_model、SDK versions、request_id、elapsed_ms。示例的空 `raw_result` 仅为省略写法；实际返回官方管线的完整 JSON 结果。

- `local_id` 只在本页本次解析内唯一，不是后续数据库主键。
- `ordinal` 保留管线块列表顺序；`reading_order` 为上游值，可能为 null，不能假造连续阅读顺序。
- bbox 为 `[x1,y1,x2,y2]`，相对于**上传后的页面图片**。关闭自动旋转/矫正；带 EXIF 旋转的输入先由调用方规范化，否则拒绝。PDF 点坐标转换由后端以后根据渲染信息完成。
- 保留表格 HTML/公式字符串及 raw_result，不在此阶段解析合并单元格、跨页表格或推断单元格坐标。原始内容是数据，前端不能直接作为可信 HTML 插入。
- `status=ok` 表示管线返回了结构有效的结果，**不表示 OCR 内容准确或全文完整**。0.2.0 对固定的 PaddleX 3.7.2 使用实例级响应适配，保留每个区域的 `finish_reason`：出现 `length` 为 `truncated=true`；全部为 `stop` 才为 false；无结果或未知原因仍为 null。适配不兼容时启动失败，不猜测“未截断”。后端拒绝发布截断页，并保留失败页和原始响应供显式重试。
- `OCR_IMAGE_TEXT=true` 默认识别图片区域内文字；不会自动理解任意流程图的箭头、空间关系或复杂图表。返回对应警告，不能把文字顺序当成已验证的流程关系。
- 空块返回 `no_blocks_detected`，不擅自断定空白页。人工验收需包含中文、英文、表格、公式及扫描页。

## 资源与失败

一个服务实例同时处理一个页面，区域 OCR 并发默认 2；繁忙返回 429，不在内存中排长队。默认限制 8 MiB 上传、1600 万像素、8 MiB 结果、180 秒页面耗时，超限返回错误，不裁剪正文伪装成功。

布局模型在隔离子进程中常驻。单页超时或客户端断开会终止该子进程，下一请求重新加载；不会终止共享的远端 vLLM。断开 HTTP **不能保证远端 GPU 推理立即停止**，调用方不能无上限立即重试。

常见错误：401 鉴权失败；413 上传超限；415 格式不支持；422 输入无效；429 忙；502 管线结果无效；503 worker/vLLM 不可用；504 页面超时。只记录请求标识、页号、耗时和状态，不记录图像、识别正文、密钥或上游完整异常。

服务不实现持久化队列、自动重试、跨页检查点和文档发布。那些能力按改造计划由后端承担。
