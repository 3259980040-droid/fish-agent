"""Resource-owned original/crop/frame views; image data is shown to the main model."""
import base64
import hashlib
import io
import json
from pathlib import Path
from PIL import Image, ImageOps
from doc_store import ingest, doc_detail, library_root

NATIVE_IMAGE_BYTES = 32 * 1024 * 1024


class ImageViews:
    def __init__(self, root):
        self.root = Path(root)

    def register(self, path, *, scope='owner', source='image_observation', reference=''):
        path = Path(path).resolve()
        hasher = hashlib.sha256()
        with path.open('rb') as file:
            for block in iter(lambda:file.read(1024 * 1024),b''):
                hasher.update(block)
        digest = hasher.hexdigest()
        with Image.open(path) as image:
            width, height = image.size
            metadata = {'sha256': digest, 'width': width, 'height': height,
                        'frames': getattr(image, 'n_frames', 1), 'source_reference': reference}
        return ingest(self.root, path.name[:120], '', kind='image', scope=scope, source=source,
                      resource_key='image:' + digest, original_path=str(path), metadata=metadata)

    def view(self, resource, *, scope='owner', mode='original', box=None, frame=0, include_pixels=True):
        record = doc_detail(self.root, resource, scope=scope)
        original = Path(record['original_path'])
        if record['kind'] != 'image' or not original.is_file():
            raise ValueError('这个资源没有可读取的图片原件')
        path = original
        with Image.open(original) as source:
            frames = getattr(source, 'n_frames', 1)
            if not 0 <= int(frame) < frames:
                raise ValueError('指定帧不在动图范围内')
            source.seek(int(frame))
            size = source.size
            orientation = source.getexif().get(274, 1)
            if mode not in ('original', 'crop', 'frame'):
                raise ValueError('图片视图应为original、crop或frame')
            if mode == 'crop' or mode == 'frame' or source.format not in ('PNG','JPEG','WEBP') or orientation != 1:
                image = ImageOps.exif_transpose(source).convert('RGB')
                if mode == 'crop':
                    if not isinstance(box, list) or len(box) != 4:
                        raise ValueError('局部需要原图像素坐标[x0,y0,x1,y1]')
                    x0,y0,x1,y1 = [int(value) for value in box]
                    if not (0 <= x0 < x1 <= image.width and 0 <= y0 < y1 <= image.height):
                        raise ValueError('局部坐标超出当前图片范围')
                    image = image.crop((x0,y0,x1,y1))
                key = hashlib.sha256(json.dumps([mode,box,frame,orientation]).encode()).hexdigest()[:16]
                path = library_root(self.root, record['scope']) / 'views' / record['doc_id'] / (key + '.png')
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    image.save(path, 'PNG')
                size = image.size
        uri = self.uri(path) if include_pixels else ''
        return {'resource_id': record['doc_id'], 'scope': record['scope'], 'original_path': str(original),
                'view_path': str(path), 'mode': mode, 'box': box, 'frame': int(frame), 'frames': frames,
                'width': size[0], 'height': size[1], 'original_metadata': record['metadata'],
                '_fish_images': [{'type': 'text', 'text': '[实际图片视图] ' + json.dumps({'resource_id': record['doc_id'],
                    'mode': mode, 'box': box, 'frame': int(frame)}, ensure_ascii=False)},
                    {'type': 'image_url', 'image_url': {'url': uri, 'detail': 'high'}}]}

    @staticmethod
    def uri(path):
        path = Path(path)
        if path.stat().st_size > NATIVE_IMAGE_BYTES:
            raise ValueError('原件超过当前模型单图协议；原件已保留，请用局部或帧视图取用')
        with Image.open(path) as source:
            mime = {'PNG':'image/png', 'JPEG':'image/jpeg', 'WEBP':'image/webp'}.get(source.format)
            if mime is None or source.getexif().get(274, 1) != 1:
                buffer = io.BytesIO()
                ImageOps.exif_transpose(source).convert('RGB').save(buffer, 'PNG')
                data, mime = buffer.getvalue(), 'image/png'
            else:
                data = path.read_bytes()
        if len(data) > NATIVE_IMAGE_BYTES:
            raise ValueError('此视图超过当前DeepSeek图像单件32 MiB协议；原件已保留，请使用局部或帧视图')
        return 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')

    def preview(self, resource, *, scope='owner', mode='original', box=None, frame=0):
        view = self.view(resource, scope=scope, mode=mode, box=box, frame=frame, include_pixels=False)
        view.pop('_fish_images')
        with Image.open(view['view_path']) as source:
            display = ImageOps.exif_transpose(source).convert('RGB')
            display.thumbnail((800, 800))
            data = io.BytesIO(); display.save(data, 'JPEG', quality=86)
        return {**view, 'preview': 'data:image/jpeg;base64,' + base64.b64encode(data.getvalue()).decode('ascii'),
                'preview_display_only': True}
