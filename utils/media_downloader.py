"""下载克隆商品的图片与视频到本地静态目录，保证素材完整可用。

闲鱼详情接口只返回图片/视频的外链（img.alicdn.com / xianyu-video.alicdn.com），
外链可能过期或带防盗链。一键克隆时把媒体真正下载到 ``static/uploads/clone``，
再把素材库里的 URL 换成本服务可访问的本地路径。

下载失败不阻断克隆：单个文件失败只记录日志，其余照常入库。
"""

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
from loguru import logger

from utils.user_agents import CHROME_UA


# 与 reply_server 的 /static 挂载保持一致：static_dir/uploads/clone/<item_id>/
STATIC_ROOT = Path(__file__).resolve().parents[1] / "static"
CLONE_DIR = STATIC_ROOT / "uploads" / "clone"
URL_PREFIX = "/static/uploads/clone"

MAX_IMAGES = 9
MAX_VIDEOS = 1
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024
DOWNLOAD_TIMEOUT = 60

_HEADERS = {
    "User-Agent": CHROME_UA,
    "Referer": "https://www.goofish.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,video/*,*/*;q=0.8",
}


def _safe_item_id(item_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", str(item_id or ""))[:40] or "unknown"


def _ext_from_url(url: str, default: str) -> str:
    path = urlparse(url).path.lower()
    match = re.search(r"\.(jpg|jpeg|png|webp|gif|mp4)$", path)
    return match.group(1) if match else default


def _ishttp(url: Any) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://", "//"))


def _local_url(item_id: str, filename: str) -> str:
    return f"{URL_PREFIX}/{item_id}/{filename}"


async def _fetch(session: aiohttp.ClientSession, url: str, max_bytes: int) -> Optional[bytes]:
    if url.startswith("//"):
        url = "https:" + url
    async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as resp:
        if resp.status != 200:
            logger.warning(f"媒体下载失败 HTTP {resp.status}: {url[:120]}")
            return None
        content_type = resp.headers.get("Content-Type", "")
        data = await resp.read()
        if not data or ("text/html" in content_type and len(data) < 2048):
            logger.warning(f"媒体下载内容无效: {url[:120]}")
            return None
        if len(data) > max_bytes:
            logger.warning(f"媒体过大({len(data)}B): {url[:120]}")
            return None
        return data


async def download_item_media(detail: Dict[str, Any]) -> Dict[str, Any]:
    """把 detail 里的远程图片/视频下载到本地，返回带本地 URL 的 detail 副本。

    返回的新 detail 中：
      - images: 本地 URL 列表（失败的项被丢弃）
      - videos: [{url, cover, poster}] 本地 URL（url=视频，cover/poster=封面）
      - media_download: {images_ok, images_fail, videos_ok, videos_fail}
    """
    result = dict(detail)
    item_id = _safe_item_id(detail.get("item_id"))
    target = CLONE_DIR / item_id
    target.mkdir(parents=True, exist_ok=True)

    raw_images = [u for u in (detail.get("images") or []) if _ishttp(u)][:MAX_IMAGES]
    raw_videos = [v for v in (detail.get("videos") or []) if isinstance(v, dict)][:MAX_VIDEOS]

    local_images: List[str] = []
    local_videos: List[Dict[str, str]] = []
    ok_img = fail_img = ok_vid = fail_vid = 0

    connector = aiohttp.TCPConnector(limit=6, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        # 图片并发下载，保持原顺序
        async def dl_image(idx: int, url: str):
            nonlocal ok_img, fail_img
            ext = _ext_from_url(url, "jpg")
            try:
                data = await _fetch(session, url, MAX_IMAGE_BYTES)
            except Exception as exc:
                logger.warning(f"图片下载异常 {url[:80]}: {type(exc).__name__}")
                data = None
            if data:
                # 内容 hash 进文件名：视频源/图片变了 URL 就变，绕开浏览器缓存
                digest = hashlib.md5(data).hexdigest()[:8]
                filename = f"img_{idx + 1}_{digest}.{ext}"
                (target / filename).write_bytes(data)
                return _local_url(item_id, filename), True
            fail_img += 1
            return None, False

        img_results = await asyncio.gather(*(dl_image(i, u) for i, u in enumerate(raw_images)))
        for url, ok in img_results:
            if ok and url:
                local_images.append(url)
                ok_img += 1

        # 视频（含封面）
        for idx, video in enumerate(raw_videos):
            vurl = video.get("url") or ""
            if not _ishttp(vurl):
                fail_vid += 1
                continue
            ext = _ext_from_url(vurl, "mp4")
            try:
                data = await _fetch(session, vurl, MAX_VIDEO_BYTES)
            except Exception as exc:
                logger.warning(f"视频下载异常 {vurl[:80]}: {type(exc).__name__}")
                data = None
            if not data:
                fail_vid += 1
                continue
            vdigest = hashlib.md5(data).hexdigest()[:8]
            vname = f"video_{idx + 1}_{vdigest}.{ext}"
            (target / vname).write_bytes(data)

            cover_url = ""
            cover = video.get("cover") or video.get("poster") or ""
            if _ishttp(cover):
                try:
                    cdata = await _fetch(session, cover, MAX_IMAGE_BYTES)
                except Exception:
                    cdata = None
                if cdata:
                    cext = _ext_from_url(cover, "jpg")
                    cdigest = hashlib.md5(cdata).hexdigest()[:8]
                    cname = f"video_{idx + 1}_cover_{cdigest}.{cext}"
                    (target / cname).write_bytes(cdata)
                    cover_url = _local_url(item_id, cname)
            local_videos.append({
                "url": _local_url(item_id, vname),
                "cover": cover_url,
                "poster": cover_url,
            })
            ok_vid += 1

    result["images"] = local_images
    result["videos"] = local_videos
    result["media_download"] = {
        "images_ok": ok_img, "images_fail": fail_img,
        "videos_ok": ok_vid, "videos_fail": fail_vid,
    }
    logger.info(
        f"商品 {item_id} 媒体下载完成：图片 {ok_img}/{ok_img + fail_img} 成功，"
        f"视频 {ok_vid}/{ok_vid + fail_vid} 成功"
    )
    return result
