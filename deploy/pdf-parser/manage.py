import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sys

DEPLOY = Path(__file__).resolve().parent
ROOT = DEPLOY.parents[1]
MODELS = {
    "OCR_LAYOUT_MODEL_DIR": ("PaddlePaddle/PP-DocLayoutV3", "7b48a7566925fa464281f930c58eee04fe2c862a"),
    "OCR_VLLM_MODEL_DIR": ("PaddlePaddle/PaddleOCR-VL-1.6", "c5630abae1d940eafe0697512a0325494b02ab42"),
}


def load_env(path):
    """解析部署环境文件，拒绝重复键及不合规的配置项。"""
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise ValueError(f"Invalid configuration at line {number}")
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Invalid quoting at line {number}")
            value = value[1:-1]
        values[key] = value
    for name in MODELS:
        if values.get(name):
            values[name] = str((path.parent / values[name]).resolve())
    return values


def initialize(path, gpu=None):
    """以独占创建方式生成部署配置和随机密钥，保留已有配置。"""
    content = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    if gpu is not None:
        if not re.fullmatch(r"\d+", gpu):
            raise ValueError("GPU must be one numeric device ID")
        content = content.replace("OCR_CUDA_VISIBLE_DEVICES=\n", f"OCR_CUDA_VISIBLE_DEVICES={gpu}\n")
    for name in ("PDF_PARSER_API_KEY", "OCR_VLLM_API_KEY"):
        content = content.replace(name + "=\n", name + "=" + secrets.token_urlsafe(48) + "\n")
    # O_EXCL 保证重复初始化不会覆盖已有密钥或配置。
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    print("Configuration created. Edit GPU selection and network binding before starting.")


def prepare_models(values):
    """准备固定版本的模型文件，拒绝复用不受管理的目标目录。"""
    from huggingface_hub import snapshot_download

    for key, (repository, revision) in MODELS.items():
        target = Path(values[key])
        manifest = target / "deployment-model.json"
        expected = {"repository": repository, "revision": revision}
        # 只续传同一固定版本的受管权重，避免把已有目录中的其他模型混入当前部署。
        if target.exists() and any(target.iterdir()):
            if not manifest.is_file() or json.loads(manifest.read_text(encoding="utf-8")) != expected:
                raise ValueError("Model directory already contains unmanaged or different weights; choose a new directory")
        target.mkdir(parents=True, exist_ok=True)
        if not manifest.exists():
            with manifest.open("x", encoding="utf-8") as stream:
                json.dump(expected, stream, indent=2)
        snapshot_download(repo_id=repository, revision=revision, local_dir=str(target),
                          ignore_patterns=["*.md", ".gitattributes"])
        print(json.dumps({"model": repository, "revision": revision, "download": "complete"}))


def launch_command(mode, values):
    """检查启动资源和配置，生成隔离代理环境的服务启动命令。"""
    environment = os.environ.copy()
    environment.update(values)
    key_names = ("PDF_PARSER_API_KEY", "OCR_VLLM_API_KEY") if mode == "parser" else ("OCR_VLLM_API_KEY",)
    for name in key_names:
        value = values.get(name, "")
        if len(value) < 32 or not value.isascii() or any(c.isspace() for c in value):
            raise ValueError(f"Missing or invalid {name}; initialize configuration first")
    gpu = values.get("OCR_CUDA_VISIBLE_DEVICES", "")
    if not re.fullmatch(r"\d+", gpu):
        raise ValueError("Choose one GPU in OCR_CUDA_VISIBLE_DEVICES after checking available memory")
    environment["CUDA_VISIBLE_DEVICES"] = gpu
    # 仅影响此服务，避免继承终端的错误代理和调试正文日志设置。
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environment.pop(name, None)
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    port_name = "PDF_PARSER_PORT" if mode == "parser" else "OCR_VLLM_PORT"
    if not 1 <= int(values[port_name]) <= 65535:
        raise ValueError(f"Invalid {port_name}")
    if mode == "parser":
        environment["PYTHONPATH"] = str(ROOT / "addons" / "pdf-parser")
        command = [sys.executable, "-m", "uvicorn", "pdf_parser.main:app", "--workers", "1",
                   "--host", values["PDF_PARSER_HOST"], "--port", values["PDF_PARSER_PORT"],
                   "--no-access-log"]
    else:
        model = Path(values["OCR_VLLM_MODEL_DIR"])
        if not (model / "config.json").is_file():
            raise ValueError("Prepare OCR_VLLM_MODEL_DIR before starting")
        fraction = float(values["OCR_VLLM_GPU_MEMORY_UTILIZATION"])
        if not 0 < fraction <= 0.9:
            raise ValueError("Invalid OCR_VLLM_GPU_MEMORY_UTILIZATION")
        if not 4096 <= int(values["OCR_VLLM_MAX_MODEL_LEN"]) <= 32768:
            raise ValueError("Invalid OCR_VLLM_MAX_MODEL_LEN")
        if not 1 <= int(values["OCR_VLLM_MAX_NUM_SEQS"]) <= 8:
            raise ValueError("Invalid OCR_VLLM_MAX_NUM_SEQS")
        environment["VLLM_API_KEY"] = values["OCR_VLLM_API_KEY"]
        environment["VLLM_NO_USAGE_STATS"] = "1"
        environment["VLLM_DEBUG_LOG_API_SERVER_RESPONSE"] = "0"
        command = [sys.executable, "-m", "vllm.entrypoints.openai.api_server", "--model", str(model),
                   "--served-model-name", values["OCR_VLLM_MODEL"], "--trust-remote-code",
                   "--host", values["OCR_VLLM_HOST"], "--port", values["OCR_VLLM_PORT"],
                   "--gpu-memory-utilization", values["OCR_VLLM_GPU_MEMORY_UTILIZATION"],
                   "--max-model-len", values["OCR_VLLM_MAX_MODEL_LEN"],
                   "--max-num-seqs", values["OCR_VLLM_MAX_NUM_SEQS"],
                   "--max-num-batched-tokens", "16384", "--no-enable-prefix-caching",
                   "--mm-processor-cache-gb", "0", "--disable-fastapi-docs"]
    return command, environment


def main():
    """分派初始化、模型准备和服务启动命令，以受控信息报告失败。"""
    parser = argparse.ArgumentParser(description="Independent OCR deployment; never modifies backend configuration")
    parser.add_argument("command", choices=["init", "prepare-models", "vllm", "parser"])
    parser.add_argument("--env-file", type=Path, default=DEPLOY / ".env")
    parser.add_argument("--from-environment", action="store_true", help="Container mode; no .env read")
    parser.add_argument("--gpu", help="GPU ID to write only when initializing a new configuration")
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize(args.env_file.resolve(), args.gpu)
            return 0
        values = dict(os.environ) if args.from_environment else load_env(args.env_file.resolve())
        if args.command == "prepare-models":
            prepare_models(values)
            return 0
        if sys.platform != "linux":
            raise ValueError("Production launch requires Linux; use check.py for client-side tests")
        command, environment = launch_command(args.command, values)
        os.execve(sys.executable, command, environment)
    except FileExistsError:
        print("Configuration already exists; nothing overwritten.", file=sys.stderr)
    except (ValueError, KeyError):
        # 不输出异常内容：第三方异常或非法配置可能包含凭证。
        print("Invalid or incomplete configuration. Check .env against README and template.", file=sys.stderr)
    except Exception as exc:
        print(f"Deployment failed: {type(exc).__name__}; configuration and credentials not printed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
