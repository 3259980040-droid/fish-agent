"""Prepare the official local DeepSeek V4 tokenizer for a source installation."""
from pathlib import Path
import io
import json
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))
import dependencies  # noqa: E402,F401
from network_config import session  # noqa: E402


def prepare(root=ROOT):
    target = Path(root) / 'vendor/tokenizers/deepseek-v4'
    target.mkdir(parents=True, exist_ok=True)
    if (target / 'tokenizer.json').is_file():
        return target
    url = 'https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip'
    with session(Path(root)) as connection:
        response = connection.get(url, timeout=45)
        response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for entry in archive.infolist():
            name = Path(entry.filename).name
            if name in ('tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json'):
                temporary = target / (name + '.tmp')
                temporary.write_bytes(archive.read(entry))
                temporary.replace(target / name)
    if not (target / 'tokenizer.json').is_file():
        raise RuntimeError('官方压缩包没有tokenizer.json，分词器未就绪')
    (target / 'source.json').write_text(json.dumps({'family': 'deepseek-v4', 'source': url,
        'documentation': 'https://api-docs.deepseek.com/quick_start/token_usage/'}, indent=2), encoding='utf-8')
    return target


if __name__ == '__main__':
    print(prepare())
