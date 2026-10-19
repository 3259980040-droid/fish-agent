"""Run the private bundled ffmpeg/video analyzer; no QQ or model calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))
sys.path.insert(0, str(ROOT))
from multimodal import MediaService
from fish_desktop.bootstrap import initialize


def main():
    out = ROOT / 'artifacts/round2'; out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='video-fixture-', dir=out) as temporary:
        root = Path(temporary)
        initialize(root)
        video = root / 'fixture.mp4'
        ffmpeg = ROOT / 'vendor/runtime/video/node_modules/ffmpeg-static/ffmpeg.exe'
        subprocess.run([str(ffmpeg), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=640x360:rate=12', '-t', '4', '-pix_fmt', 'yuv420p', str(video)], check=True, timeout=30)
        media = MediaService(root, 'research')
        media.config['video']['command'] = str(ROOT / 'vendor/runtime/video/node_modules/mcp-video-analyzer/dist/index.js')
        frames = root / 'extracted'; frames.mkdir()
        started = time.monotonic(); data = media.extract_video(str(video), frames)
        images = [*frames.rglob('*.jpg'), *frames.rglob('*.png')]
        if not images: (out / 'video-errors.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        assert images, '实际视频工具未导出图片'
        cancel = threading.Event(); timer = threading.Timer(.2, cancel.set); timer.start(); target = root / 'cancelled'; target.mkdir()
        cancelled = False
        try: media.extract_video(str(video), target, cancel)
        except InterruptedError: cancelled = True
        timer.join()
        result = {'ok': bool(images) and cancelled, 'real_video_extraction': True, 'frame_files': len(images), 'cancelled': cancelled,
            'analyzer_version': '0.11.0', 'elapsed_s': round(time.monotonic()-started, 2), 'warnings': data.get('warnings', []), 'model_calls': 0, 'real_qq_sends': 0}
        (out / 'video-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
        # Windows media/indexing handles can close just after the process exits.
        time.sleep(1)


if __name__ == '__main__': main()
