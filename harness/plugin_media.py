"""Turn standard MCP image results into role-private, validated image files."""
import base64
import io
from pathlib import Path
import uuid
from PIL import Image, ImageOps
from storage import role_dir


def normalize_result(root, role, result):
    if not isinstance(result, dict) or not isinstance(result.get('content'), list): return result
    converted = []
    count = 0
    for block in result['content']:
        if not isinstance(block, dict): continue
        if block.get('type') == 'image':
            count += 1
            if count > 6: raise ValueError('一次插件结果最多包含 6 张图片')
            encoded = str(block.get('data', ''))
            if len(encoded) > 28 * 1024 * 1024: raise ValueError('插件返回的图片过大')
            try:
                value = base64.b64decode(encoded, validate=True)
                with Image.open(io.BytesIO(value)) as im:
                    if im.width * im.height > 40_000_000: raise ValueError('插件图片像素过大')
                    image = ImageOps.exif_transpose(im).convert('RGB'); image.thumbnail((1440, 1440))
                    directory = role_dir(root, role) / 'plugin-results'; directory.mkdir(exist_ok=True)
                    path = directory / (uuid.uuid4().hex + '.jpg'); image.save(path, 'JPEG', quality=86)
            except (OSError, Image.DecompressionBombError) as exc:
                raise ValueError('插件返回的图片无法解码') from exc
            converted.append({'type': 'image_file', 'path': str(path), 'mediaType': 'image/jpeg', 'name': path.name})
        elif block.get('type') == 'audio':
            converted.append({'type': 'text', 'text': '插件返回音频；当前桥接尚未转写该音频，不能声称已经听懂。'})
        else:
            converted.append(block)
    return {**result, 'content': converted}
