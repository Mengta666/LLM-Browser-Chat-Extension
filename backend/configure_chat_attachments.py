"""仅补齐附件配置；签名密钥在本地生成，不输出到终端。"""

import argparse
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True, help='模型容器可访问的后端根地址，不含 /v1')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    parsed = urlsplit(base)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        parser.error('base-url 必须是 HTTP(S) 根地址，不能包含凭证、路径或查询参数')
    target = Path(__file__).resolve().parent / 'config' / '.env'
    if not target.is_file():
        parser.error('请先从 backend/config/.env.example 创建 backend/config/.env')
    existing = dotenv_values(target)
    fields = {
        'CHAT_ATTACHMENT_BASE_URL': ('模型可访问的 Chat 图片下载根地址，不含 /v1。', base),
        'CHAT_ATTACHMENT_SIGNING_KEY': ('独立随机签名密钥；持久保存，不提交、不打印。', secrets.token_urlsafe(32)),
        'CHAT_ATTACHMENT_URL_TTL_SECONDS': ('签名链接有效秒数；默认 900，允许 600～86400。', '900'),
    }
    added = []
    for key, (comment, value) in fields.items():
        if existing.get(key):
            continue
        if key not in existing:
            with target.open('a', encoding='utf-8') as stream:
                stream.write(f'\n# {comment}\n{key}=\n')
        set_key(target, key, value, encoding='utf-8')
        added.append(key)
    print('已补齐字段：' + ', '.join(added) if added else '附件配置已存在，未修改。')
    print('现有非空配置保持不变。重启后端后生效。')


if __name__ == '__main__':
    main()
