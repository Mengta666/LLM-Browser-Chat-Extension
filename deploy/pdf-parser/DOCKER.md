# Docker / WSL 部署 PDF 解析服务

当前选择此方式。**不再安装宿主机 vLLM/Paddle 虚拟环境**，也不改现有后端、知识库数据库或其他模型服务。已创建的虚拟环境无需删除。

适用：已确认的 WSL、RTX 3080 20 GiB、Windows 驱动 572.16（报告支持 CUDA 12.8）。这说明 WSL 可看到 GPU，**不等于 Docker GPU 或 OCR 已经通过验证**。

## 1. 先确认 Docker GPU 可用

在 WSL 执行：

```bash
docker version
docker compose version
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu22.04 nvidia-smi
```

最后一条需要首次拉取 CUDA 测试镜像。只有容器内也能看到 GPU 才继续；它仍只是驱动可见性测试，不是模型推理测试。

如果使用 Docker Desktop：启动 Desktop、选择 Linux containers / WSL 2 engine，并在 Settings → Resources → WSL Integration 中启用当前发行版。不要在 WSL 内另装 Linux NVIDIA 显示驱动。参考 [Docker Desktop GPU 支持](https://docs.docker.com/desktop/features/gpu/)、[NVIDIA WSL 指南](https://docs.nvidia.com/cuda/wsl-user-guide/index.html)。

如果已有独立运行于 WSL 的 Docker Engine，继续使用已有引擎及其 NVIDIA Container Toolkit，不同时安装第二套 daemon。`docker version` 只有 Client、没有 Server，或提示找不到命令时，先修复 Docker 连接/集成，不继续安装模型依赖。

要求支持 GPU reservation 的 Compose v2（下文 `up --wait` 使用 Compose 2.20+）。[Compose GPU 配置依据](https://docs.docker.com/compose/how-tos/gpu-support/)。

## 2. 进入包含部署文件的项目根目录

在当前本地项目对应的 WSL 路径中执行：

```bash
cd "/mnt/d/桌面/browser_agent/browser-agent"
```

如果部署在另一台机器，复制 `addons/pdf-parser/` 和 `deploy/pdf-parser/`，保持这两层目录关系，然后进入它们共同的根目录。当前未提交的文件无法通过 `git pull` 获得；不要拷贝后端真实 `.env`、本地虚拟环境或业务数据。

此 Compose 使用独立项目名 `browser-agent-ocr`，不与搜索增强的 `deploy/compose.yaml` 混用；不包含 OpenWebUI、SearXNG 或后端。

## 3. 生成配置

```bash
python3.12 deploy/pdf-parser/manage.py init --gpu 0
```

只用 Python 标准库，宿主机不用安装 pip 依赖。自动生成两个密钥和 `OCR_CUDA_VISIBLE_DEVICES=0`，保存在被忽略的 `deploy/pdf-parser/.env`，不打印密钥。重复运行会拒绝覆盖；已有该文件时直接编辑 GPU 参数，不删除配置重新生成。

本机 Windows 后端访问 WSL/Docker Desktop 时，先保持默认发布 `127.0.0.1:19082`。其他机器需要访问时再将 `PDF_PARSER_HOST` 改为 Docker 宿主机可绑定的内网地址或 `0.0.0.0`，并限制客户端 IP、配置必要的 WSL 网络转发。不能把另一台服务器的 `10.8.125.20` 地址原样填到这台机器。

Docker 中的连接关系固定为：

```text
宿主机 127.0.0.1:19082
  → pdf-parser:8000（鉴权、版面检测、OCR 结果组装）
  → ocr-vllm:8000/v1（仅容器网络内访问，不发布 18083）
  → PaddleOCR-VL-1.6
```

`.env` 中 `OCR_VLLM_BASE_URL`、`OCR_VLLM_HOST/PORT` 和模型目录用于原生部署，不覆盖 Compose 的容器内地址。两个容器均请求物理 GPU 0，内部使用映射后的 `gpu:0`。不要给解析服务填写 `http://127.0.0.1:18083/v1`，那会指向解析容器自身。

初始显存比例保持 `0.25`（约占这张卡总显存的四分之一，不是剩余显存比例）；布局模型另外占显存。若实际启动不足，再结合错误和显存调整，不能预先保证每种页面都在这个配置下完成。

## 4. 校验、构建和下载模型

以下命令依次执行，失败即停。只使用 `config --quiet`，不要把展开的带密钥配置发到聊天中。

```bash
docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml config --quiet
docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml build pdf-parser ocr-vllm
docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml --profile tools run --rm --no-deps models
```

第一次构建会下载较多依赖。解析镜像以 NVIDIA CUDA 12.6.3 / cuDNN / Ubuntu 24.04 为基础，在容器内安装 Python 3.12、PaddlePaddle GPU 3.2.1 和项目固定依赖；vLLM 使用独立 CUDA 12.8.1 基础镜像，安装官方 PyPI `vllm==0.11.1` x86 wheel 和明确固定的 PyTorch `2.9.0+cu128`，不安装到同一环境，也不在宿主机安装 Python 依赖。

**不要将官方 Docker 标签与同版本 PyPI wheel 的 CUDA 版本混为一谈。** `vllm/vllm-openai:v0.11.1` 发布镜像实际使用 CUDA 12.9.1，在 Windows 驱动 572.16 上已复现 `unsatisfied condition: cuda>=12.9`。本部署因此构建 `browser-agent-ocr-vllm:0.11.1-cu128`；不设置 `NVIDIA_DISABLE_REQUIRE`，也不通过替换驱动库绕过检查。依据：[官方发布构建配置](https://github.com/vllm-project/vllm/blob/v0.11.1/.buildkite/release-pipeline.yaml)、[官方默认 wheel CUDA 版本](https://github.com/vllm-project/vllm/blob/v0.11.1/vllm/envs.py)。

若已初始化旧配置，只将部署 `.env` 中的 `OCR_VLLM_IMAGE` 改为 `browser-agent-ocr-vllm:0.11.1-cu128`，保留原有密钥，不能重新运行 `init` 覆盖它。新增的 `OCR_VLLM_CUDA_IMAGE` 未填写时会使用 Compose 的 12.8.1 默认值。

`models` 是一次性准备服务，无 GPU 申请、无业务密钥、不会启动后端。它下载 `manage.py` 中固定 revision 的 PP-DocLayoutV3 原生 Paddle 权重和 PaddleOCR-VL-1.6，写入本项目的 `ocr-models` 命名卷。默认完整卷名为 `browser-agent-ocr_ocr-models`。

**先成功运行 models，再启动推理服务**。下载中断可以显式重跑同一命令；不匹配的旧模型目录会被拒绝混写。正常运行时模型卷只读，并禁用隐式联网下载。网络需能访问官方镜像源、PyPI/Paddle wheel 源和 Hugging Face；下载失败不自动切换未知镜像或模型版本。

基础镜像和主要依赖固定到版本，但尚未锁定所有传递依赖及基础镜像 digest。验收后将实际镜像 ID、RepoDigest 和依赖清单记录为部署基线；不要直接切换 `latest`。镜像构建成功不等于 GPU 推理验收通过。

## 5. 启动与检查

```bash
docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml up -d --wait --wait-timeout 900 ocr-vllm pdf-parser
docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml ps
```

先等待 vLLM 模型健康，再启动解析服务。基础配置采用 `restart: "no"`，首次验收失败时不会反复重启占用 GPU；常驻主机验收后启用第 9 节的恢复配置。

再次检查解析服务与实际模型名：

```bash
docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml exec -T pdf-parser python deploy/pdf-parser/check.py --from-environment --parser-url http://127.0.0.1:8000 --vlm-url http://ocr-vllm:8000/v1
```

检查宿主机发布端口（WSL 中执行，不需要安装依赖）：

```bash
python3.12 deploy/pdf-parser/check.py --parser-url http://127.0.0.1:19082
```

此时 `inference_verified=false` 是正常且诚实的结果：健康检查没有执行页面识别。

## 6. 显式真实 PDF 单页测试

把你要测试的 PDF 放在部署主机本地。先将指定材料复制进解析容器的临时目录，下面的 `/path/to/test.pdf` 必须换成实际路径：

```bash
docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml cp /path/to/test.pdf pdf-parser:/tmp/ocr-test.pdf
docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml exec -T pdf-parser python deploy/pdf-parser/check.py --from-environment --parser-url http://127.0.0.1:8000 --pdf /tmp/ocr-test.pdf --page 1 --save-result /tmp/ocr-page-1.json
```

测试材料仅临时存于该容器，重建容器可能丢失；不进入知识库或长期记忆。已存在的 JSON 结果不覆盖，重复测试换新文件名。可追加 `--expect '页面上的中文短语' --expect 'English text'`，检查中英文关键内容。

需要人工核对时，下载结果到一个**尚不存在的本地文件名**：

```bash
mkdir -p deploy/pdf-parser/runtime
docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml cp pdf-parser:/tmp/ocr-page-1.json deploy/pdf-parser/runtime/ocr-page-1.json
```

复制前确认目标不存在，避免 `docker cp` 覆盖旧结果；真实材料和结果不要提交到仓库。请将测试工件存放到已忽略的 `test/` 或部署目录的 `runtime/` 下，而不是项目根目录长期留存。

至少验证中文、英文双栏、混排、表格和公式页面；关键页重复 3 次。结构检查和字面命中不等于完整准确的 OCR。解析服务 0.2.0 保留上游区域结束原因：`length` 表示截断，全部 `stop` 表示本次生成未撞长度限制，其他情况保留未知；这不保证识别无遗漏。`OCR_IMAGE_TEXT=true` 开启图片内文字识别，不保证箭头关系正确。

## 7. 日志、停止与故障

```bash
docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml logs --tail 80 ocr-vllm pdf-parser
docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml stop
```

`stop` 只停止此 OCR 项目的容器，保留模型卷。不执行 `down -v`、`docker system prune` 或删除旧服务。日志轮转为每服务 3×10 MiB，不启用请求正文/识别正文日志；不要粘贴 `.env` 或 `docker inspect` 的环境变量输出。

| 现象 | 处理方向 |
| --- | --- |
| WSL 能看到 GPU，但 Docker 不行 | 检查 Desktop WSL 集成或已有 Engine 的 NVIDIA runtime；不是 Python 依赖问题 |
| `config --quiet` 提示变量缺失 | 检查 `.env` 是否初始化、GPU 是否填 0；不要输出密钥 |
| models 下载失败 | 查看错误类型、网络、命名卷归属；不要删除成功下载的数据 |
| vLLM 退出 / CUDA OOM | 查看 vLLM 日志及显存，检查是否还有原生 OCR 占同一端口或 GPU |
| `vlm_model_not_found` | 模型名是否仍为 `PaddlePaddle/PaddleOCR-VL-1.6` |
| 宿主机可用但其他机器不可用 | 检查端口发布地址、WSL NAT/镜像网络、Windows 防火墙；不是改容器内 BASE_URL |
| 504 或结果为空 | 区分复杂页超时与 OCR 失败，保留测试图像复现，不以 HTTP 200 代替识别验收 |

接口和资源限制见 [解析服务说明](../../addons/pdf-parser/README.md)。原生环境部署仍保留在 [README.md](README.md) 作为替代路径，但不要与 Docker 同时启动。

## 8. 已验收的 WSL NAT 内网访问方式

2026-09-28 在远端 Windows + Ubuntu-24.04 WSL2 上部署到 `/opt/docker/vllm/ocr-server`。以下为已落地状态，不需要重复创建规则：

```text
客户端 10.8.125.10
  → Windows VPN 地址 10.8.125.11:19082（防火墙仅放行该客户端）
  → Windows portproxy → 127.0.0.1:19082
  → WSL localhost forwarding
  → Docker pdf-parser:8000 → 内部 ocr-vllm:8000/v1
```

这台机器已实测 Windows 能访问 WSL 的 `127.0.0.1:19082`，因此转发目标使用本机回环地址，不写死会变化的 WSL NAT 地址。该方式依赖 WSL 的 localhost forwarding；其他机器必须先验证本机回环访问，不能直接照搬。依据：[Microsoft WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)。

- 当前客户端接口：`http://10.8.125.11:19082`；`GET /health` 无鉴权，`GET /ready` 和 `POST /v1/parse/page` 要求 Bearer 密钥。
- Windows 防火墙规则名：`Browser-Agent-OCR-19082`；监听地址仅 `10.8.125.11`，远端来源仅 `10.8.125.10`。
- Compose 仍发布 WSL 的 `127.0.0.1:19082`，vLLM 不发布宿主机端口。
- 密钥只保存在远端 `deploy/pdf-parser/.env`；不要在聊天中粘贴，也不要提交。客户端换机器/IP 时需单独调整来源限制。
- Windows 的端口转发和防火墙规则持久化。原始部署不含自启；2026-10-06 增加了第 9 节的可选恢复机制。仅 SSH 可连接仍不代表 OCR 已就绪。

在部署机 WSL 中恢复启动（保持现有配置及模型卷）：

```bash
cd /opt/docker/vllm/ocr-server
sudo docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml up -d --wait --wait-timeout 900 ocr-vllm pdf-parser
```

本次 13 次容器内页面识别及 1 次跨机器鉴权调用成功；中文、英文双栏、混排表格和扫描公式关键页重复测试。表格单元格及双栏顺序通过核对，但扫描公式仍有字符/符号误识别，不能据此保证 OCR 全文准确。健康状态、HTTP 成功、重复结果一致与识别准确率是不同指标。

验收工件保存在部署目录 `deploy/pdf-parser/runtime/acceptance-20260928/`，包括原始识别 JSON、实际依赖清单及镜像 ID。它们不进入 Git，也未写入知识库或长期记忆；本阶段尚未把主项目 PDF 入库链路切到此服务。

## 9. Windows / WSL 常驻与故障恢复

`instanceIdleTimeout=-1` 只防止 WSL 空闲退出，不能抵挡升级、手动终止或系统重启。Microsoft Store 可以后台更新 WSL；不应靠关闭系统更新保证 OCR 可用。

恢复分两层：

1. `compose.recovery.yaml` 给两个常驻容器启用 `unless-stopped`。Docker 启动或容器异常退出后恢复容器；`models` 仍不自动执行。
2. Windows 任务 `Browser-Agent-OCR-Recovery` 在开机后 30 秒及每 2 分钟运行，以**发行版所属用户**的 S4U 身份启动指定 WSL，再执行 `recover.sh`。不保存 Windows 密码，不使用看不到该用户发行版的 SYSTEM 账户。
3. 指定 `-ListenAddress` 后，同一任务在 OCR 健康时检查本机回环接口、VPN 地址监听及转发后的健康接口。VPN 尚未取得地址时记录 `network=address_pending`，后续巡检继续尝试；监听缺失时只重新注册该地址、端口到 `127.0.0.1` 同端口的转发。防火墙规则必须预先配置，本脚本不扩大放行范围、不重启共享 IP Helper 服务。

任务仅恢复已存在的容器，不构建、不拉镜像、不下载模型、不重建或删除容器/卷。两服务健康时不修改它们；启动中等待下次检查；容器缺失或暂停时报错留待人工处理。对已判定 unhealthy 的 OCR 容器进行局部重启，对停止的容器按 Compose 依赖恢复。单次恢复等待最多约 16 分钟，相邻恢复尝试至少间隔 10 分钟；健康检查不受该冷却限制。任务不并发执行，也不会唤醒休眠的 Windows。

### 安装

前提：已完成基础部署，两容器存在；WSL 已启用 systemd，Docker 随该发行版启动；镜像与模型均在本地。S4U 在不同 Windows/域策略下可能受限，必须实际运行并检查结果，不能只看任务创建成功。

在 WSL 部署根目录检查叠加配置，并仅更新现有两容器的重启策略：

```bash
cd /opt/docker/vllm/ocr-server
sudo docker compose --env-file deploy/pdf-parser/.env -f deploy/pdf-parser/compose.yaml -f deploy/pdf-parser/compose.recovery.yaml config --quiet
sudo docker update --restart unless-stopped browser-agent-ocr-ocr-vllm-1 browser-agent-ocr-pdf-parser-1
sudo bash deploy/pdf-parser/recover.sh
```

上面容器名对应默认项目名；自定义项目名时先核对 `compose ps -a`，不能操作其他项目。以后显式重建本服务时也要带 `-f deploy/pdf-parser/compose.recovery.yaml`，否则基础配置会恢复 `restart: no`。

把同目录的 `install-wsl-recovery.ps1` 和 `recover-wsl.ps1` 复制到 Windows 本地目录，以**发行版所有者的管理员 PowerShell**执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-wsl-recovery.ps1 -Distribution Ubuntu-24.04 -DeploymentRoot /opt/docker/vllm/ocr-server -ListenAddress 10.8.125.11 -ListenPort 19082
Start-ScheduledTask -TaskName Browser-Agent-OCR-Recovery
```

安装器拒绝覆盖同名任务或已有安装目录，避免误改现存配置。脚本复制到 `C:\ProgramData\BrowserAgentOCR`，仅管理员、SYSTEM 和安装用户可写；Linux 恢复脚本仍位于指定部署目录。首次部署必须检查实际任务结果、外部 `/health`、带鉴权 `/ready` 和一次页面识别。

两个 PowerShell 脚本使用 UTF-8 BOM，以兼容 Windows PowerShell 5.1 对中文注释的读取。已有任务升级时先备份脚本与导出的任务 XML，等待任务结束后更新脚本，在原动作参数中追加 `-ListenAddress` 和 `-ListenPort`，保留原用户身份、开机及周期触发器，再实际运行验证；不要重复运行安装器覆盖已有任务。

### 查询和维护暂停

```powershell
Get-ScheduledTaskInfo -TaskName Browser-Agent-OCR-Recovery
Get-Content C:\ProgramData\BrowserAgentOCR\recovery.log -Tail 10
```

任务退出码 0 不一定已经就绪：`warming_up` 表示仍在加载，`healthy` / `recovered_healthy` 才表示容器健康检查通过。配置转发检查时，还需 `network=healthy` 或 `network=recovered`；`loopback_unhealthy`、`portproxy_repair_failed`、`forward_health_failed` 分别指向回环服务、转发注册及转发后接口故障。远端本机的接口检查不能代替客户端跨机器访问及实际 OCR 识别。日志仅保留受控状态与退出码，1 MiB 轮转，不记录密钥或原文。Linux 冷却记录位于 `deploy/pdf-parser/runtime/recovery/`。

**手动停服前先停用恢复任务**，并等当前执行结束，否则下一次巡检会拉起已停止的容器：

```powershell
Disable-ScheduledTask -TaskName Browser-Agent-OCR-Recovery
Get-ScheduledTask -TaskName Browser-Agent-OCR-Recovery | Select-Object State
```

确认不再处于 `Running` 后，才执行所需的 Compose stop 或 WSL terminate。维护结束：

```powershell
Enable-ScheduledTask -TaskName Browser-Agent-OCR-Recovery
Start-ScheduledTask -TaskName Browser-Agent-OCR-Recovery
```

Linux 侧也可创建 `deploy/pdf-parser/runtime/recovery/paused` 暂停容器恢复，但它不能阻止 Windows 任务启动 WSL；需要整个发行版停机时应停用 Windows 任务。回退恢复机制时先停用任务，再把这两个容器改回 `docker update --restart no`；不需要删除容器、模型或 `.env`。

本机制不改变 Windows 休眠、系统更新、网络防火墙或 VPN。宿主机关机/休眠、VPN 未连接、磁盘/GPU 故障期间仍可能不可用；启动触发器存在不等于已经完成 Windows 整机重启验收。

### 本机部署验收记录（2026-10-06，远端 UTC+08 已为 10 月 7 日）

- 已核实最终任务脚本与仓库文件 SHA256 一致，S4U 任务实际运行成功。
- 经授权受控停止一次 `Ubuntu-24.04`，外部接口确实断开；未手动启动 WSL，下一次定时任务于远端 12:36:36 执行，12:36:43 记录模型加载中，随后两个原容器恢复健康。
- Docker 容器 ID、镜像 ID、模型卷及原始 `compose.yaml` 均保持不变；后续定时检查未重启健康容器。
- 从客户端经 `10.8.125.11:19082` 调用 `/ready` 及合成中英文图片识别，连续 3 次识别通过 5 项关键字检查，耗时约 8.57 / 1.68 / 1.67 秒。无鉴权 `/ready` 返回 401。未读取用户简历、未写入知识库或长期记忆。
- 脚本的健康、维护暂停、启动中、容器缺失、停止、unhealthy、冷却及 Docker 启动失败等分支另有 9 项模拟控制测试通过；这不代替上述真实恢复和推理验收，也不代表每种故障均完成真实注入。
- **未执行 Windows 整机重启，也未再触发一次真实 WSL 升级**；这两种完整场景仍待单独验收。当前证明的是受控 WSL 停止后自动恢复。

客户端验收记录：`test/pdf-parser-deploy/recovery-live-20261006.json`；远端任务日志：`C:\ProgramData\BrowserAgentOCR\recovery.log`。合成材料的关键字通过不等于任意 PDF 识别准确。

### 端口转发恢复验收（2026-10-19，远端 UTC+08）

- 故障时两个容器均健康，Windows 回环 `/health` 返回 200，防火墙规则仍正确；但持久化 portproxy 规则没有对应的 VPN 地址监听。单独 `set` 未恢复，重新注册这一条规则后恢复。
- 已更新 `C:\ProgramData\BrowserAgentOCR\recover-wsl.ps1` 和原任务动作参数，保留 S4U 身份、开机后 30 秒与每 2 分钟触发器。旧脚本和任务 XML 备份后缀为 `20261019-112737`，保存在同一受限目录。
- 受控移除这一条转发后，客户端连接确实超时；未手动运行恢复脚本，下一次周期任务于 11:28:44 记录 `status=healthy network=recovered exit=0`，重新建立 VPN 监听。
- 客户端鉴权 `/ready` 返回 `ready`；合成中英文图片识别通过 4 项关键字及页面结构检查，返回 3 个块、未截断，推理耗时约 9.05 秒。未读取用户文档或写入知识库。
- 未执行 Windows 整机重启；实际开机与 VPN 启动先后顺序仍待重启验收。当前验证覆盖真实任务身份、周期触发、转发缺失恢复以及跨机器 OCR 调用。
