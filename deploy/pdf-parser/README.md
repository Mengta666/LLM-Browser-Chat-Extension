# PDF 解析部署：PaddleOCR-VL-1.6＋PP-DocLayoutV3

本步骤只部署独立解析能力，**不会切换现有知识库解析器，不改数据库、真实后端 `.env` 或已有模型服务**。先部署并验证，下一阶段再接入后端。

**当前选择 Docker / WSL 部署，直接使用 [DOCKER.md](DOCKER.md)。宿主机不必安装下面的 vLLM/Paddle 环境。** 本文保留原生 Python 部署作为替代路径，不与 Docker 同时启动。

两种方式都不包含 OpenWebUI，都隔离 OCR 推理和解析环境。以下为原生部署说明：

```text
后端机器（后续接入）
  ① 渲染 PDF 为单页 PNG，保留页号和渲染尺寸
  ② 携带解析服务密钥上传页面
               │ HTTP :19082
               ▼
模型服务器：pdf-parser（PaddleOCR/PaddlePaddle 环境）
  ③ PP-DocLayoutV3 检出版面块
  ④ 管线处理各区域，调用 vLLM
               │ 本机 HTTP 127.0.0.1:18083/v1
               ▼
模型服务器：vLLM（独立环境）
  ⑤ PaddleOCR-VL-1.6 识别文字、表格、公式
               │ 返回区域结果
               ▼
  ⑥ pdf-parser 组装原始 JSON 和规范化块
               │ 返回页结果
               ▼
  ⑦ 后端保存检查点、入库、建索引（尚未实施）
```

## 1. 准备目录与硬件

面向 Linux x86_64、Python **3.12**、NVIDIA GPU。沿用原服务器，不要求升级现有服务。

将本地以下两个目录复制到服务器，保持层级；例如根目录为 `/opt/browser-agent-ocr`：

```text
/opt/browser-agent-ocr/
├── addons/pdf-parser/        # 服务源码和 requirements.txt
└── deploy/pdf-parser/        # 本说明、脚本、模板和固定依赖
```

也可以复制整个仓库。不要拷贝本地 `.venv`、真实后端 `.env`、数据库、测试材料和模型缓存。尚未提交的文件不能通过 `git pull` 获得，需要先用 SFTP/SCP 等复制上述目录。

以下命令均从这个**部署根目录**执行：

```bash
cd /opt/browser-agent-ocr
nvidia-smi
python3.12 --version
```

双 RTX 3090 并不意味着有两张空闲卡。先确认物理 GPU 编号和剩余显存，后面显式填入配置；脚本不会选择 GPU、停止其他服务或自动升级驱动。

本部署的安装基线：

| 环境 | 固定的直接依赖 | 说明 |
| --- | --- | --- |
| OCR 推理 | vLLM 0.11.1 | 官方 1.6 配方的最低兼容基线；默认 wheel 使用 CUDA 12.8 |
| 页解析 | PaddleOCR 3.7.0、PaddleX 3.7.2、PaddlePaddle GPU 3.2.1 | Paddle GPU wheel 从官方 CUDA 12.6 源安装 |
| 自检客户端 | PyMuPDF 1.28.2、Pillow 12.3.0 | PDF 渲染、图像尺寸校验；不需要 GPU |

这是**待服务器实机验收的固定组合**，不是“GPU 已验证可用”的声明，也不是完整传递依赖锁文件。部署通过后保存 `pip freeze` 与驱动版本，作为该服务器的验收基线。

vLLM 和 Paddle 的 CUDA wheel 可以位于不同虚拟环境，但共同依赖宿主机驱动兼容；`nvidia-smi` 显示的是驱动支持的 CUDA 上限，不是当前 Python wheel 的版本。若不满足 CUDA 12.8，请先确认对应 wheel 方案，不要对在用服务器直接升级驱动。依据：[vLLM 0.11.1 安装说明](https://github.com/vllm-project/vllm/blob/v0.11.1/docs/getting_started/installation/gpu.cuda.inc.md)。

## 2. 建立两个环境

```bash
python3.12 -m venv deploy/pdf-parser/runtime/venvs/vllm
python3.12 -m venv deploy/pdf-parser/runtime/venvs/parser

deploy/pdf-parser/runtime/venvs/vllm/bin/python -m pip install -r deploy/pdf-parser/requirements-vllm.txt --extra-index-url https://download.pytorch.org/whl/cu128

deploy/pdf-parser/runtime/venvs/parser/bin/python -m pip install paddlepaddle-gpu==3.2.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
deploy/pdf-parser/runtime/venvs/parser/bin/python -m pip install -r addons/pdf-parser/requirements.txt
deploy/pdf-parser/runtime/venvs/parser/bin/python -m pip install -r deploy/pdf-parser/requirements-check.txt

deploy/pdf-parser/runtime/venvs/vllm/bin/python -m pip check
deploy/pdf-parser/runtime/venvs/parser/bin/python -m pip check
```

每条命令成功后再继续；不要在已有聊天 vLLM、embedding、reranker 或后端环境中安装这些包。不要再安装 `paddlex[genai-vllm-server]` 到解析环境：它会把 vLLM 及其依赖引入同一个环境，本方案通过远程 API 调用。

Paddle GPU 安装方式来自 [PaddleOCR 官方管线文档](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PaddleOCR-VL.html)。Linux 缺少 OpenCV 动态库时，按报错补齐对应系统库；不通过降级现有服务的 Python 包来解决。

## 3. 初始化并编辑配置

```bash
python3.12 deploy/pdf-parser/manage.py init
```

仅创建不存在的 `deploy/pdf-parser/.env`，权限 0600，自动生成两份独立密钥，不打印密钥。重复运行明确拒绝覆盖，密钥文件已被 Git 忽略。

编辑该文件，至少确认：

```dotenv
# 示例：明确选物理 GPU 0；请按实际空闲卡修改，不是自动选择。
OCR_CUDA_VISIBLE_DEVICES=0

# 后端在其他机器时，绑定服务器实际内网 IP；示例对应现有模型服务器。
PDF_PARSER_HOST=10.8.125.20
PDF_PARSER_PORT=19082

# OCR vLLM 保持本机可用，不需要外部访问。
OCR_VLLM_HOST=127.0.0.1
OCR_VLLM_PORT=18083
OCR_VLLM_BASE_URL=http://127.0.0.1:18083/v1
```

`PDF_PARSER_HOST=0.0.0.0` 也能外部访问，但必须用防火墙限制后端 IP，跨不可信网络加 VPN 或 HTTPS。**0.0.0.0 是监听地址，客户端使用服务器真实 IP**。Bearer 密钥不应明文走公网。

默认 vLLM 显存比例 `0.25` 是整卡总显存的比例，不是剩余显存比例；还要为 PP-DocLayoutV3 及已有模型留空间。默认并发较低：1 个页面、2 个区域、2 个 vLLM sequence。资源不足先使用 `OCR_LAYOUT_DEVICE=cpu` 运行版面检测，或另选 GPU；不要无条件把显存比例调成 0.9。

模板内每个参数均有说明。密钥通过子进程环境传递，不拼到启动命令参数中。启动器只对自身子进程移除代理环境，避免内网调用被错误代理拦截，不修改系统代理。

## 4. 显式下载固定版本模型

```bash
deploy/pdf-parser/runtime/venvs/parser/bin/python deploy/pdf-parser/manage.py prepare-models
```

需要访问 Hugging Face。首次下载产生 `deploy/pdf-parser/runtime/models/`，此目录被忽略，不提交权重。固定 revision 写在 `manage.py`，同时生成每个模型的 `deployment-model.json`：

| 模型 | revision |
| --- | --- |
| `PaddlePaddle/PP-DocLayoutV3` | `7b48a7566925fa464281f930c58eee04fe2c862a` |
| `PaddlePaddle/PaddleOCR-VL-1.6` | `c5630abae1d940eafe0697512a0325494b02ab42` |

其中 PP-DocLayoutV3 使用 `inference.json/.pdiparams/.yml` 的 **Paddle 原生权重**，不要替换成 Transformers 的 `_safetensors` 仓库。

下载中断可以重新执行同一命令；若目标目录里有不属于本部署的文件或不同 revision，脚本拒绝混写。网络不通时可在能访问的机器下载相同 revision，再连同 manifest 复制到上述目录；不要顺手切换未知镜像或模型版本。常规启动启用离线模式，不自动下载权重。

固定模型来源：[PP-DocLayoutV3](https://huggingface.co/PaddlePaddle/PP-DocLayoutV3/tree/7b48a7566925fa464281f930c58eee04fe2c862a)、[PaddleOCR-VL-1.6](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6/tree/c5630abae1d940eafe0697512a0325494b02ab42)。

## 5. 先启动 vLLM，再启动解析服务

终端 A：

```bash
deploy/pdf-parser/runtime/venvs/vllm/bin/python deploy/pdf-parser/manage.py vllm
```

等待模型加载完成。终端 B：

```bash
deploy/pdf-parser/runtime/venvs/parser/bin/python deploy/pdf-parser/manage.py parser
```

必须使用各自环境的 Python。解析服务先核对 vLLM 模型名、加载布局模型，再监听端口，首次初始化需要时间。不要启动多个 uvicorn worker 来提速，每个 worker 都会加载模型和建立独立并发。

启动参数按 [官方 1.6 vLLM 配方](https://recipes.vllm.ai/PaddlePaddle/PaddleOCR-VL-1.6)设置，关闭 prefix caching 和多模态 processor cache。服务端名称显式统一为 `PaddlePaddle/PaddleOCR-VL-1.6`；不可只改一边，否则 readiness 拒绝通过。

先用前台终端观察启动，不自动注册开机服务。验收后如需常驻，可把这两条命令交给现有 systemd/supervisor 管理；设置绝对工作目录、独立非特权用户和日志轮转，不使用多个解析 worker。停止前台进程使用 Ctrl+C，仅停止对应的新服务。

## 6. 分层自检

先检查模型名和服务就绪（**不会执行 OCR**）：

```bash
deploy/pdf-parser/runtime/venvs/parser/bin/python deploy/pdf-parser/check.py --parser-url http://127.0.0.1:19082 --vlm-url http://127.0.0.1:18083/v1
```

若解析服务只绑定 `10.8.125.20`，把 `--parser-url` 改成 `http://10.8.125.20:19082`，不要仍用 127.0.0.1。

然后显式用自己的测试 PDF 做真实单页解析：

```bash
deploy/pdf-parser/runtime/venvs/parser/bin/python deploy/pdf-parser/check.py --parser-url http://10.8.125.20:19082 --pdf /path/to/test.pdf --page 1 --save-result deploy/pdf-parser/runtime/page-1.json
```

客户端使用 144 DPI 渲染选定页面，不上传整个 PDF、不入库、不写长期记忆。也可直接传 `--image /path/to/page.png`。输出文件采用独占创建，已有文件不会被覆盖；换一个结果文件名即可复测。

如果明确知道页面包含某些短语，追加 `--expect '中文测试' --expect 'English'`（换成该页真实内容），让脚本检查两种语言的字面命中。脚本默认只输出状态、长度、块数、耗时，不打印正文或密钥。

从另一台机器检查：复制 `deploy/pdf-parser/` 下的脚本和配置模板、安装 `requirements-check.txt`，在该机器私密 `.env` 中填入解析服务的同一密钥及相同模型名。**仅检测解析服务时无需提供 vLLM 密钥，不开放它的端口**；省略 `--vlm-url`。文件路径是检查脚本所在机器的本地路径，不是解析服务器文件路径。

验收至少覆盖：中文扫描、英文双栏、中英混排、表格合并单元格、公式和公式编号。复杂页面重复 3 次，人工对照保存的 `blocks/raw_result` 与页面，记录耗时和 GPU 峰值。`page_inference.ok=true` 仍只是结构/非空/指定短语检查通过，不等于全部文字准确。

0.2.0 固定适配 PaddleX 3.7.2，保留区域 `finish_reason`。任一 `length` 返回 `truncated=true`，全部 `stop` 为 false，未知仍为 null；后端不会发布已知截断页。false 仅表示生成未撞输出上限，不保证 OCR 无遗漏。图片内文字识别默认开启（`OCR_IMAGE_TEXT=true`），空间关系仍须原页核对。

部署通过后，保存两个环境的 `pip freeze`、`nvidia-smi` 和自检结果到私有运维记录，再接入后端。不要上传真实材料或 `.env`。

## 7. 故障定位

| 现象 | 先检查 |
| --- | --- |
| 外部连接拒绝 | 实际监听 IP、19082 防火墙；不要改 vLLM 为公网监听来解决 |
| `vlm_model_not_found` | vLLM 暴露模型名与 `OCR_VLLM_MODEL` 是否完全一致 |
| 解析服务启动失败 | 两个 pip check、Paddle GPU wheel、布局权重目录、vLLM 是否先启动 |
| CUDA/OOM | 驱动和 wheel、显存、设备映射；减少并发或改布局为 CPU，不停止其他模型 |
| 401 | 使用的是解析密钥还是 vLLM 密钥；配置文件路径是否正确 |
| 429 | 服务已有页面在处理，稍后显式重试，不增加无限队列 |
| 413/422 | 图片字节/像素、格式、EXIF 旋转、页号；降低渲染 DPI 或修正图像 |
| 504 | 页面解析超时，检查复杂页与 vLLM 状态；服务会回收布局进程，下次重新加载 |
| `no_content_recognized` | 是否真为空白页、图像是否清晰、模型是否实际识别；不是 API 正常就算成功 |

上游进程的正文与完整异常日志被抑制以保护材料，HTTP 层只记录受控状态。需要深入诊断时在**独立测试终端、非敏感测试图像**上按官方示例复现，不打开生产正文调试日志。

超时/客户端断线会终止解析子进程，但不保证远端 vLLM 的 GPU 计算立即停止。避免立即并发反复重试。本阶段不承诺整份 PDF 的可靠恢复，文档队列和页级检查点将在后端改造中实现。

完整接口说明见 [addons/pdf-parser/README.md](../../addons/pdf-parser/README.md)。
