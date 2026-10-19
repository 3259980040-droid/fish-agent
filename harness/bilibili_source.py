"""Bilibili Source Adapter（Phase 2 最小版）。

只做收藏夹读取，不做搜索/评论/点赞/关注：
  list_favorites / list_items / get_video / get_subtitle

真实模式用 requests 直连 Bilibili Web API；测试用 FixtureBilibiliAdapter。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

API = "https://api.bilibili.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class SourceError(RuntimeError):
    pass


class SourceAuthError(SourceError):
    pass


def load_credential(root: Path) -> dict[str, str]:
    """从 data/research/bili_credential.json 或环境变量读 B 站凭证。"""
    data: dict[str, str] = {}
    from secrets_store import SecretStore
    protected = SecretStore(root).get('bili-account')
    if protected:
        data = json.loads(protected)
    path = root / "data" / "research" / "bili_credential.json"
    if path.exists() and not protected:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {str(k).lower(): str(v) for k, v in raw.items() if v}
                if data and os.name == 'nt':
                    SecretStore(root).put('bili-account', json.dumps(data))
                    path.unlink()  # successful DPAPI migration replaces plaintext
        except (OSError, ValueError):
            data = {}
    for key, env in (
        ("sessdata", "BILI_SESSDATA"),
        ("bili_jct", "BILI_JCT"),
        ("buvid3", "BILI_BUVID3"),
        ("dedeuserid", "BILI_DEDEUSERID"),
    ):
        value = os.environ.get(env)
        if value:
            data[key] = value
    return data


class BilibiliSourceAdapter:
    def __init__(self, root: Path, credential: dict[str, str] | None = None):
        self.root = root
        self.credential = credential if credential is not None else load_credential(root)
        self.session = __import__('network_config').session(root)
        headers = {
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }
        cookie = "; ".join(
            f"{cookie_name}={self.credential[key]}"
            for key, cookie_name in (
                ("sessdata", "SESSDATA"),
                ("bili_jct", "bili_jct"),
                ("buvid3", "buvid3"),
                ("dedeuserid", "DedeUserID"),
            )
            if self.credential.get(key)
        )
        self.session.headers.update(headers)
        self.api_headers = {'Cookie': cookie} if cookie else {}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(API + path, params=params, headers=self.api_headers, timeout=20)
        if response.status_code == 412:
            raise SourceError("Bilibili 412 风控")
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise SourceError(f"Bilibili API {payload.get('code')}: {payload.get('message')}")
        return payload.get("data") or {}

    def _sdk(self):
        import plugin_registry  # initializes the private SDK dependency directory
        import bilibili_api
        cfg = __import__('network_config').settings(self.root)
        bilibili_api.request_settings.set_proxy(cfg['proxy'])
        bilibili_api.request_settings.set('trust_env', cfg['mode'] == 'system')
        bilibili_api.request_settings.set_timeout(20)
        bilibili_api.request_settings.set_wbi_retry_times(1)
        return bilibili_api

    @staticmethod
    def _video_row(item):
        import html, re
        duration = item.get('duration', item.get('length', 0)) or 0
        if isinstance(duration, str):
            parts = duration.split(':')
            try:
                seconds = 0
                for part in parts: seconds = seconds * 60 + int(part)
                duration = seconds
            except ValueError: duration = 0
        bvid = str(item.get('bvid', ''))
        owner = item.get('owner') or {}
        return {'bvid': bvid, 'title': html.unescape(re.sub('<[^>]+>', '', str(item.get('title', bvid)))),
                'url': 'https://www.bilibili.com/video/' + bvid,
                'author': item.get('author') or owner.get('name', ''), 'mid': item.get('mid') or owner.get('mid', ''),
                'duration': int(duration), 'description': item.get('description', ''), 'cover': item.get('pic', '')}

    def search(self, keyword, limit=12):
        sdk = self._sdk()
        data = sdk.sync(sdk.search.search_by_type(keyword=keyword, search_type=sdk.search.SearchObjectType.VIDEO,
            order_type=sdk.search.OrderVideo.PUBDATE, page=1, page_size=min(42, limit)))
        return [self._video_row(v) for v in data.get('result', [])[:limit] if v.get('bvid')]

    def up_videos(self, uid, limit=12):
        sdk = self._sdk()
        credential = sdk.Credential(**{k: v for k, v in self.credential.items() if k in ('sessdata', 'bili_jct', 'buvid3', 'dedeuserid')})
        data = sdk.sync(sdk.user.User(int(uid), credential=credential).get_videos(ps=min(30, limit)))
        return [{**self._video_row(v), 'mid': str(uid)} for v in data.get('list', {}).get('vlist', [])[:limit]]

    def recommendations(self, limit=20):
        sdk = self._sdk()
        credential = sdk.Credential(**{k: v for k, v in self.credential.items() if k in ('sessdata', 'bili_jct', 'buvid3', 'dedeuserid')})
        data = sdk.sync(sdk.homepage.get_videos(credential))
        return [self._video_row(v) for v in data.get('item', [])[:limit] if v.get('bvid')]

    def login_status(self):
        try:
            info = self._get('/x/web-interface/nav')
            return {'ok': True, 'logged_in': bool(info.get('isLogin')), 'name': str(info.get('uname', '')), 'uid': str(info.get('mid', ''))}
        except SourceError:
            return {'ok': True, 'logged_in': False, 'name': '', 'uid': ''}

    def list_favorites(self, uid: int = 0) -> list[dict[str, Any]]:
        if not uid:
            uid = int(self.credential.get("dedeuserid") or 0)
        if not uid:
            raise SourceAuthError("需要 B 站登录凭证（DedeUserID），才能读取自己的收藏夹")
        data = self._get("/x/v3/fav/folder/created/list-all", {"up_mid": uid})
        result = []
        for item in data.get("list", []) or []:
            result.append({
                "media_id": int(item.get("id", 0)),
                "title": item.get("title", ""),
                "media_count": int(item.get("media_count", 0)),
            })
        return result

    def list_items(self, media_id: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get("/x/v3/fav/resource/list", {
                "media_id": int(media_id),
                "pn": page,
                "ps": 20,
                "platform": "web",
                "order": "mtime",
                "type": 0,
                "tid": 0,
            })
            for item in data.get("medias", []) or []:
                bvid = item.get("bvid", "")
                if not bvid:
                    continue
                items.append({
                    "bvid": bvid,
                    "title": item.get("title", ""),
                    "author": (item.get("upper") or {}).get("name", ""),
                    "duration": int(item.get("duration", 0)),
                    "cover": item.get("cover", ""),
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "fav_time": int(item.get("fav_time", 0)),
                })
            if not data.get("has_more"):
                break
            page += 1
            if page > 50:
                break
        return items

    def get_video(self, bvid: str) -> dict[str, Any]:
        data = self._get("/x/web-interface/view", {"bvid": bvid})
        pages = data.get("pages", []) or []
        cid = int(pages[0].get("cid", 0)) if pages else int(data.get("cid", 0))
        return {
            "bvid": bvid,
            "title": data.get("title", ""),
            "author": (data.get("owner") or {}).get("name", ""),
            "duration": int(data.get("duration", 0)),
            "cover": data.get("pic", ""),
            "cid": cid,
            "url": f"https://www.bilibili.com/video/{bvid}",
            "pubdate": int(data.get("pubdate", 0)),
        }

    def get_subtitle(self, bvid: str, cid: int) -> dict[str, Any] | None:
        data = self._get("/x/player/v2", {"bvid": bvid, "cid": cid})
        subtitles = ((data.get("subtitle") or {}).get("subtitles") or [])
        if not subtitles:
            return None
        target = subtitles[0]
        url = target.get("subtitle_url") or ""
        if not url:
            return None
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = urljoin("https://www.bilibili.com", url)
        response = self.session.get(url, timeout=20)
        response.raise_for_status()
        payload = response.json()
        body = payload.get("body", []) or []
        def timestamp(seconds):
            seconds = max(0, int(float(seconds or 0)))
            return f'{seconds//3600:02d}:{seconds%3600//60:02d}:{seconds%60:02d}'
        text = "\n".join(
            '[' + timestamp(item.get('from')) + '] ' + str(item.get("content", "")).strip()
            for item in body
            if str(item.get("content", "")).strip()
        )
        if not text:
            return None
        return {"text": text, "lang": target.get("lan", ""), "url": url, 'segments': body}

    def get_play_url(self, bvid: str, cid: int) -> str | None:
        """取一个可直接下载的 mp4 地址；用于无字幕时喂给本地视频理解。"""
        data = self._get("/x/player/playurl", {
            "bvid": bvid,
            "cid": cid,
            "qn": 64,
            "fnval": 1,
            "fnver": 0,
            "fourk": 0,
            "platform": "html5",
            "high_quality": 1,
        })
        durl = data.get("durl") or []
        if durl:
            return durl[0].get("url")
        return None

    def download_video(self, bvid: str, cid: int) -> Path | None:
        url = self.get_play_url(bvid, cid)
        if not url:
            return None
        dest = self.root / "data" / "research" / "videos" / f"{bvid}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 1024:
            return dest
        tmp = dest.with_suffix(".mp4.part")
        try:
            with self.session.get(
                url, stream=True, timeout=120,
                headers={"Referer": f"https://www.bilibili.com/video/{bvid}"},
            ) as response:
                response.raise_for_status()
                size = 0
                with open(tmp, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > 500 * 1024 * 1024:
                            raise SourceError("视频超过 500MB，放弃下载")
                        handle.write(chunk)
            if tmp.stat().st_size < 1024:
                raise SourceError("下载文件过小")
            tmp.replace(dest)
            return dest
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


class FixtureBilibiliAdapter:
    """离线验收用：数据格式见 fixtures/research_favorites.json。"""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.favorites = data.get("favorites", []) or []
        self.items = data.get("items", {}) or {}

    def list_favorites(self, uid: int = 0) -> list[dict[str, Any]]:
        return list(self.favorites)

    def list_items(self, media_id: int) -> list[dict[str, Any]]:
        return list(self.items.get(str(media_id), []))

    def get_video(self, bvid: str) -> dict[str, Any]:
        for rows in self.items.values():
            for item in rows:
                if item.get("bvid") == bvid:
                    return {
                        "bvid": bvid,
                        "title": item.get("title", ""),
                        "author": item.get("author", ""),
                        "duration": int(item.get("duration", 0)),
                        "cover": item.get("cover", ""),
                        "cid": int(item.get("cid", 0)),
                        "url": item.get("url") or f"https://www.bilibili.com/video/{bvid}",
                    }
        raise SourceError(f"fixture 中找不到 {bvid}")

    def get_subtitle(self, bvid: str, cid: int) -> dict[str, Any] | None:
        for rows in self.items.values():
            for item in rows:
                if item.get("bvid") == bvid:
                    text = item.get("subtitle")
                    if text:
                        return {"text": str(text), "lang": "zh-CN", "url": ""}
                    return None
        return None
