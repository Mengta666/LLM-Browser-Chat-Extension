"""只创建缺失的本地部署配置，不操作容器或修改后端配置。"""

import argparse
import os
from pathlib import Path
import secrets


def initialize(directory: Path, with_search=False):
    created = []
    env = directory / ".env"
    if not env.exists():
        content = (directory / ".env.example").read_text(encoding="utf-8")
        content = content.replace("WEB_READER_API_KEY=\n", "WEB_READER_API_KEY=" + secrets.token_hex(32) + "\n")
        descriptor = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        created.append(".env")
    if with_search:
        target = directory / "runtime" / "searxng" / "settings.yml"
        if not target.exists():
            content = (directory / "searxng" / "settings.yml.example").read_text(encoding="utf-8")
            content = content.replace("__GENERATED_SECRET__", secrets.token_hex(32))
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
            created.append("runtime/searxng/settings.yml")
    return created


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-search", action="store_true", help="同时初始化本项目的 SearXNG 配置")
    args = parser.parse_args()
    created = initialize(Path(__file__).resolve().parent, args.with_search)
    print("Created: " + ", ".join(created) if created else "Existing configuration unchanged.")
    print("Secrets are stored locally and are not printed. Existing files are never overwritten.")
