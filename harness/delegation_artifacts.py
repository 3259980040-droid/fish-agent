"""Validate declared task outputs and reuse the existing attachment archive."""
import base64
import json
import mimetypes
from pathlib import Path
from attachment_store import AttachmentStore, CHUNK_BYTES


def download(root, artifact, *, offset=0):
    record = AttachmentStore(root, 'qq').get(artifact['id'])
    if not record:
        raise ValueError('成果附件记录不存在')
    path = Path(record['path']).resolve()
    archive = (root / 'data/qq/attachments').resolve()
    if not path.is_relative_to(archive):
        raise ValueError('成果附件归档位置无效')
    if type(offset) is not int or not 0 <= offset <= path.stat().st_size:
        raise ValueError('文件读取位置无效')
    with path.open('rb') as handle:
        handle.seek(offset)
        data = handle.read(CHUNK_BYTES)
    end = offset + len(data)
    return {'name': record['name'], 'media_type': mimetypes.guess_type(record['name'])[0] or 'application/octet-stream',
            'data': base64.b64encode(data).decode('ascii'), 'size': path.stat().st_size, 'offset': offset,
            'next_offset': end if end < path.stat().st_size else None, 'path': str(path), 'resource_id': record['resource_id']}
