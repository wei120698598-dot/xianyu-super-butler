"""闲鱼分享链接解析与商品详情抓取，用于「一键克隆商品」到素材库。

分享链接常见形态：

1. 淘口令整段文本，内嵌 ``m.tb.cn`` 短链——短链返回一个 HTML 中转页，
   目标地址写在 ``var url = '...'`` 里，需要请求一次再解析；
2. 直接的商品长链（``www.goofish.com/item?id=xxx``、
   ``h5.m.goofish.com/item?itemId=xxx&id=xxx``）；
3. 只粘了一个纯数字商品 ID。

解析出 itemId 后走买家端 ``mtop.taobao.idle.pc.detail`` 读取标题、
描述、价格、图片和分类，归一化成素材库需要的字段。
"""

import html
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import aiohttp
from loguru import logger

from utils import risk_control
from utils.user_agents import CHROME_UA
from utils.xianyu_seller_api import SellerApiError, XianyuSellerAPI

Fetcher = Callable[[str], Awaitable[Tuple[str, str]]]


class ShareLinkError(ValueError):
    """分享链接无法解析出商品 ID。"""


class ItemDetailError(RuntimeError):
    """商品详情接口调用失败或返回内容不可用。"""


# URL 结尾可能紧跟中文标点或括号，单独排除，避免把后面的说明文字吞进 URL
_URL_RE = re.compile(r"https?://[^\s<>'\"（）【】「」]+", re.IGNORECASE)
_ITEM_ID_RE = re.compile(r"(?<!\d)(\d{8,20})(?!\d)")
_ID_KEYS = ("itemId", "item_id", "id", "itemid")
_TARGET_URL_RE = re.compile(
    r"(?:var\s+url|window\.location(?:\.href)?|location\.href)\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_SHORT_HOSTS = ("m.tb.cn", "tb.cn", "m.intl.taobao.com")


def _clean_url(url: str) -> str:
    return html.unescape(url).rstrip(".,;，。、）)】」'\"").replace("&amp;", "&")


def extract_urls(text: str) -> List[str]:
    """从整段分享文案里取出所有 URL。"""
    return [_clean_url(match.group(0)) for match in _URL_RE.finditer(text or "")]


def item_id_from_url(url: str) -> Optional[str]:
    """从 URL 的查询参数或路径里提取商品 ID。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    query = parse_qs(parsed.query)
    for key in _ID_KEYS:
        for value in query.get(key, []):
            if re.fullmatch(r"\d{8,20}", value or ""):
                return value
    # /item/1234567890、/item?id=...之外的路径形式兜底
    for part in reversed([segment for segment in parsed.path.split("/") if segment]):
        match = re.match(r"(?:item[-_]?)?(\d{8,20})", part, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _is_short_link(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == short or host.endswith(f".{short}") for short in _SHORT_HOSTS)


def extract_target_url(page_html: str) -> Optional[str]:
    """从 m.tb.cn 中转页里解析真实目标地址。"""
    if not page_html:
        return None
    match = _TARGET_URL_RE.search(page_html)
    return _clean_url(match.group(1)) if match else None


def _build_link(item_id: str, share_url: str = "", resolved_url: str = "") -> Dict[str, str]:
    source_url = f"https://www.goofish.com/item?id={item_id}"
    return {
        "item_id": item_id,
        "source_url": source_url,
        "share_url": share_url or source_url,
        "resolved_url": resolved_url or source_url,
    }


async def _default_fetch(url: str) -> Tuple[str, str]:
    """请求短链，返回（最终 URL, 页面 HTML）。"""
    timeout = aiohttp.ClientTimeout(total=12)
    headers = {"user-agent": CHROME_UA, "accept": "text/html,application/xhtml+xml"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers, allow_redirects=True) as response:
            return str(response.url), await response.text(errors="ignore")


async def resolve_share_link(text: str, fetcher: Optional[Fetcher] = None) -> Dict[str, str]:
    """把分享文案解析成商品 ID 及各类链接。

    Args:
        text: 用户粘贴的整段分享文案、单个链接或纯数字商品 ID。
        fetcher: 可选的网络请求函数（返回最终 URL 与 HTML），便于测试注入。

    Raises:
        ShareLinkError: 无法解析出商品 ID。
    """
    raw = (text or "").strip()
    if not raw:
        raise ShareLinkError("请粘贴闲鱼分享链接或商品 ID")

    urls = extract_urls(raw)
    fetch = fetcher or _default_fetch

    for url in urls:
        item_id = item_id_from_url(url)
        if item_id:
            return _build_link(item_id, share_url=url, resolved_url=url)

        if _is_short_link(url):
            try:
                final_url, page_html = await fetch(url)
            except Exception as exc:
                logger.warning(f"解析闲鱼短链失败 {url}: {exc}")
                continue
            item_id = item_id_from_url(final_url)
            if item_id:
                return _build_link(item_id, share_url=url, resolved_url=final_url)
            target_url = extract_target_url(page_html)
            if target_url:
                item_id = item_id_from_url(target_url)
                if item_id:
                    return _build_link(item_id, share_url=url, resolved_url=target_url)

    # 没有可用链接时，允许直接粘一个商品 ID
    match = _ITEM_ID_RE.search(raw)
    if match:
        return _build_link(match.group(1), share_url=urls[0] if urls else "")

    raise ShareLinkError("无法从分享链接解析出商品 ID，请确认链接完整或改用商品详情页链接")


def _to_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


def _image_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://"):
        return f"https://{url[7:]}"
    return url


def normalize_item_detail(data: Dict[str, Any], link: Dict[str, str]) -> Dict[str, Any]:
    """把买家端商品详情响应归一化成素材库与预览卡片需要的字段。"""
    item = data.get("itemDO") if isinstance(data.get("itemDO"), dict) else {}
    track = data.get("trackParams") if isinstance(data.get("trackParams"), dict) else {}
    seller = data.get("sellerDO") if isinstance(data.get("sellerDO"), dict) else {}

    images: List[str] = []
    for info in item.get("imageInfos") or []:
        url = _image_url(info.get("url") if isinstance(info, dict) else info)
        if url and url not in images:
            images.append(url)
    if not images:
        fallback = item.get("defaultPicture")
        if isinstance(fallback, dict):
            fallback = fallback.get("url")
        url = _image_url(fallback)
        if url:
            images.append(url)

    category = ""
    for label in item.get("itemLabelExtList") or []:
        if not isinstance(label, dict):
            continue
        if str(label.get("propertyText") or "") in ("分类", "类目"):
            category = str(label.get("valueText") or label.get("text") or "").strip()
            if category:
                break
    if not category:
        cat = item.get("itemCatDTO")
        if isinstance(cat, dict):
            category = str(cat.get("catName") or cat.get("name") or "").strip()

    price = _to_float(item.get("soldPrice") or item.get("defaultPrice"))
    if price is None:
        price = _to_float(item.get("originalPrice"))
    sku_rows = item.get("skuList") or item.get("idleItemSkuList") or []

    # 视频商品：详情在 itemDO.videoPlayInfo 里，主图可能是空，视频地址在
    # playUrl / sd480pUrl / ld320pUrl，封面在 url（jpg）。全部抽出来交给下载层。
    videos: List[Dict[str, str]] = []
    play = item.get("videoPlayInfo")
    if isinstance(play, dict):
        cover = _image_url(play.get("url"))
        candidates = [
            play.get("playUrl"),
            play.get("sd480pUrl"),
            play.get("ld320pUrl"),
        ]
        for candidate in candidates:
            video_url = _image_url(candidate)
            if video_url.lower().endswith(".mp4") or "video" in video_url:
                videos.append({"url": video_url, "cover": cover})
                break
        # 视频封面也是素材，补进图片列表
        if cover and cover not in images:
            images.append(cover)

    return {
        "item_id": str(link["item_id"]),
        "title": str(item.get("title") or "").strip(),
        "description": str(item.get("desc") or "").strip(),
        "price": price,
        "original_price": _to_float(item.get("originalPrice")),
        "transport_fee": _to_float(item.get("transportFee")),
        "images": images[:9],
        "videos": videos[:3],
        "category": category,
        "category_id": str(item.get("categoryId") or track.get("categoryId") or "").strip(),
        "channel_cat_id": str(track.get("channelCatId") or "").strip(),
        "seller_nick": str(seller.get("nick") or "").strip(),
        "seller_id": str(seller.get("sellerId") or track.get("sellerId") or "").strip(),
        "item_status": str(item.get("itemStatusStr") or "").strip(),
        "quantity": str(item.get("quantity") or "").strip(),
        "sku_count": len(sku_rows) if isinstance(sku_rows, list) else 0,
        "source_url": link["source_url"],
        "share_url": link["share_url"],
        "resolved_url": link["resolved_url"],
    }


async def fetch_item_detail(cookie_id: str, cookies_str: str, link: Dict[str, str]) -> Dict[str, Any]:
    """读取商品详情并归一化；链接已由 :func:`resolve_share_link` 解析。"""
    if not cookies_str:
        raise ItemDetailError("该账号 Cookie 为空，请先登录后再克隆商品")

    api = XianyuSellerAPI(cookie_id, cookies_str)
    try:
        data = await api.get_item_detail(link["item_id"])
    except risk_control.RiskControlBlocked:
        raise
    except SellerApiError as exc:
        raise ItemDetailError(f"闲鱼商品详情接口调用失败：{exc}") from exc
    except Exception as exc:
        raise ItemDetailError(f"闲鱼商品详情接口调用失败：{exc}") from exc
    finally:
        await api.close()

    if not isinstance(data, dict) or not isinstance(data.get("itemDO"), dict):
        raise ItemDetailError("闲鱼未返回商品详情，商品可能已下架或链接无效")

    detail = normalize_item_detail(data, link)
    if not detail["title"]:
        raise ItemDetailError("源商品没有标题，无法克隆")
    return detail
