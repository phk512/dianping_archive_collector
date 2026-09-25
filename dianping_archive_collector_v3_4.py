"""
大众点评相册采集器（Dianping Archive Collector）V3.1

V3.1 新增：
  1. 采集日期范围：只看 [起始日期, 截止日期] 内的照片（留空表示不限）。
     整页都不在范围内时连页面都会跳过，不会白下载。
  2. 自动获取商户名称：从相册页标题解析商户名 / 城市 / 相册照片总数，
     采集时自动写进档案库（以前这里一直是"未命名商户"）。
  3. 自动找店：批量核验店铺并按「至少经营了多少年」筛选。
     每核验一家店只需要 2 次请求（相册首页拿名称/城市/照片数，末页拿最早的照片）。
     实测：点评的列表页/搜索页/商户主页/会员页全部有风控，所以店铺链接来源是
     「浏览器另存为的列表页 HTML」或「直接粘贴链接」——这部分在界面上有说明。
  4. 末页发现改用相册内嵌的 'albumPicCount' 直接推算（实测精确），
     一家店的末页定位从"多次二分探测"降到 2 次请求。

V3.0 修复重点：图片「上传者 / 发布时间」获取不准确。

旧版（V2.9）为什么会取错（均为实测结论）：
  1. 用「从 <img> 向上 14 层找带日期的祖先 + 打分选最优」来猜卡片。多图容器一旦
     得分胜出，容器里所有图片都会被写上同一份（通常是最后一张图的）作者/时间。
  2. BeautifulSoup 缺失时静默 `except: pass`，退化成「图片 URL 前后 ±5000 字符的
     可见文本窗口」猜测；窗口跨越多张卡片，等于把邻图的作者/时间安到当前图片上。
  3. 图片与元数据靠 URL 字符串绑定，同一张图换个尺寸后缀就对不上，只能靠兜底乱猜。
  4. 只会「填空」式写库：一旦写错，重爬也永远改不回来。

V3.0 的做法：
  * 结构化解析相册卡片（li.J_list 的 .picture-info -> .info 里 /member/ 链接 + 日期 span），
    元数据只能来自「只包含这一张图片」的卡片，绝不跨卡片取值；匹配不到就留空。
  * 日期按页面真实写法解析：25-12-18 -> 2025-12-18；06-21 这类 MM-DD 按
    「当年显示 MM-DD、跨年才显示 YY-MM-DD」的实测规则补全年份（可关闭），
    同时保存 published_raw 原始文本和 year_inferred 推算标记。
  * 图片与元数据绑定：卡片内 URL 精确匹配 -> 去掉 @尺寸 后缀的文件名匹配。
  * 元数据带 metadata_confidence 入库，置信度更高的一次解析可以纠正旧值。
  * 缺少 beautifulsoup4 时明确报错，不再静默降级。
  * 顺带修复：① <script> 内嵌 JSON 里的原图/小图变体被当成独立图片
    （实测 16 张照片的页面会变成 48 个待下载地址）；② 相册卡片里的缩略图只有
    240x180，低于默认「最小边长 200」会被整批当成过小跳过，现改为优先下载
    页面内嵌数据里的原图（实测 240x180 -> 700x525 / 1280x853）。

自检（不需要登录，前两条完全离线）：
  python dianping_archive_collector_v3_1.py --check-deps
  python dianping_archive_collector_v3_1.py --selftest
  python dianping_archive_collector_v3_1.py --parse-html <保存目录>/<shop_id>/page_1/page.html
  python dianping_archive_collector_v3_1.py --probe <店铺ID>
  python dianping_archive_collector_v3_1.py --discover <浏览器另存为的列表页.html> --city 上海 --min-years 15
"""

import os
import re
import csv
import math
import sys
import time
import json
import queue
import hashlib
import threading
import sqlite3
import webbrowser
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urljoin
from html import unescape
from datetime import datetime, timedelta

import requests
from PIL import Image, ImageTk, ImageOps
import imagehash

# BeautifulSoup 用于解析相册卡片的发布者/发布时间。
# V3.0 起它是「结构化解析」的必需依赖：缺失时不再静默降级（旧版会退化成
# 按字符窗口猜测，从而把隔壁卡片的作者/时间安到当前图片上）。
# pip install beautifulsoup4
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
    BS4_ERROR = ""
except Exception as _bs4_exc:  # pragma: no cover
    BeautifulSoup = None
    BS4_AVAILABLE = False
    BS4_ERROR = str(_bs4_exc)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ============================================================
# 默认设置
# ============================================================

DEFAULT_SETTINGS = {
    # ---------- 采集延迟 ----------
    "page_delay": 1.0,
    "image_delay": 0.35,
    "request_timeout": 15,
    "image_timeout": 25,

    # ---------- 页面探测 ----------
    "max_page_discovery": 10000,
    "verify_empty_limit": 2,
    "min_images_per_valid_page": 8,
    "photos_per_page": 16,
    "max_images_per_page": 500,

    # ---------- 去重强化开关 ----------
    "enable_in_page_dedup": True,
    "enable_url_dedup": True,
    "enable_size_filter": True,
    "enable_sha256_dedup": True,
    "enable_phash_dedup": True,
    "enable_replace_higher": True,

    # ---------- 图片尺寸与阈值 ----------
    "min_image_dimension": 200,
    "phash_threshold": 4,

    # ---------- 缩略图显示 ----------
    "thumb_w": 150,
    "thumb_h": 120,

    # ---------- 上传者 / 发布时间 ----------
    # 点评相册卡片里「今年」的照片只显示 MM-DD（例如 06-21），跨年才显示 YY-MM-DD
    # （例如 25-12-18）。开启后把 MM-DD 补全为抓取当年的年份，并记录 published_raw。
    "infer_year_for_short_date": True,
    # 点评图片 CDN 的缩略图地址形如 xxx.jpg%40240w_180h_1e_1c_1l|watermark=0（240x180），
    # 去掉 @尺寸 后缀即可拿到原图（实测 240x180 -> 1280x853）。失败会自动回退缩略图。
    "use_original_image_url": True,
    # 重新采集「已完成」的页面，用本次解析结果修正历史错误元数据。
    # 配合「跨页 URL 预去重」开启时不会重新下载图片，只重写作者/时间，代价很低。
    "recrawl_completed_pages": False,

    # ---------- 采集日期范围 ----------
    # 只收集 [date_from, date_to] 之间的照片，留空表示不限。
    # 格式：2010 / 2010-06 / 2010-06-15 都可以。需要「补全 MM-DD 的年份」开启，
    # 否则页面只显示月-日的照片会被当成"日期未知"。
    "date_from": "",
    "date_to": "",
    # 日期未知（页面没给出、或无法解析）的照片是否仍然收集
    "date_filter_keep_unknown": True,

    # ---------- 自动找店 ----------
    "discover_city_keyword": "",      # 例如「上海」，留空表示不限城市
    "discover_min_years": 0,          # 最少经营年限（按相册最早照片估算），0 表示不限
    "discover_min_photos": 0,         # 相册照片数下限，0 表示不限
    "discover_limit": 300,            # 最多核验多少个候选店铺
    "discover_delay": 1.2,            # 每个候选店铺之间的延迟（秒）

    # ---------- 登录态（用于访问有风控的列表页 / 搜索页） ----------
    # 从已登录的浏览器里「Copy as cURL」粘贴进来即可，脚本会自动取出 Cookie。
    # 只在本机保存、只发给点评；失效后重新复制一次即可。
    "cookie": "",
    "user_agent": "",                 # 留空用内置 UA
    "list_url": "",                   # 列表页 / 搜索结果页网址
    "list_max_pages": 3,              # 自动翻页最多抓几页
    # 从列表页学到的「分类 / 排序 / 商圈 / 城市」对应关系（页面上写什么就存什么）
    "learned_filters": {},
}


SETTINGS_FILE = "dianping_settings.json"
APP_NAME = "Dianping Archive Collector V3.4"
APP_VERSION = "3.4"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0"
)


# 图片黑名单
IMAGE_BLACKLIST = [
    "logo", "avatar", "icon", "loading", "qrcode",
    "sprite", "emoji", "favicon",
    "nav", "header", "footer", "btn", "button",
    "bg_", "_bg", "background", "banner", "placeholder",
    "default", "ad_", "_ad", "ads", "tag",
]


# ============================================================
# 设置管理器
# ============================================================

class SettingsManager:

    def __init__(self, path=None):
        if path is None:
            path = Path(__file__).parent / SETTINGS_FILE
        self.path = Path(path)
        self.data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    if k in DEFAULT_SETTINGS:
                        # 类型校验，防止手动改坏 JSON 导致崩溃
                        default_val = DEFAULT_SETTINGS[k]
                        try:
                            if isinstance(default_val, bool):
                                self.data[k] = bool(v)
                            elif isinstance(default_val, int):
                                self.data[k] = int(v)
                            elif isinstance(default_val, float):
                                self.data[k] = float(v)
                            else:
                                self.data[k] = v
                        except Exception:
                            self.data[k] = default_val
            except Exception:
                pass

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def reset(self):
        self.data = dict(DEFAULT_SETTINGS)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


# ============================================================
# 工具函数
# ============================================================

def safe_filename(name, default="unknown"):
    if not name:
        name = default
    name = str(name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip().strip(".")
    if not name:
        name = default
    return name[:150]


def normalize_url(url):
    if not url:
        return ""
    url = url.strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            "",
            parsed.query,
            "",
        )
    )


def canonical_image_url(url):
    if not url:
        return ""
    url = normalize_url(url)
    parsed = urlparse(url)
    query = parsed.query
    query = re.sub(
        r"(^|&)(w|width|h|height|size|quality|q|thumb|thumbnail)=[^&]*",
        "",
        query,
        flags=re.I,
    )
    query = re.sub(r"&&+", "&", query).strip("&")
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", query, "")
    )


def image_url_fingerprint(url):
    if not url:
        return ""
    try:
        p = urlparse(normalize_url(url))
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return url


# 点评 / 美团 CDN 的缩略图后缀：@ 或 %40 后面紧跟数字，例如
#   ...jpg%40240w_180h_1e_1c_1l%7Cwatermark%3D0
#   ...jpg@600w_600h_1e_1c.webp
# 去掉该后缀即可得到原图。
_SIZE_SUFFIX_RE = re.compile(r"(?:%40|@)(?=\d)[^?#]*", re.I)
# 只影响尺寸/水印的查询参数，可安全丢弃
_TRANSFORM_QUERY_RE = re.compile(
    r"[?&](?:imageView2|imageMogr2|x-oss-process|watermark)=[^&#]*", re.I
)
# 页面上 <script src=...> 之类的资源不是图片（旧版会把它们当图片 URL 收进来）
_NON_IMAGE_ASSET_RE = re.compile(
    r"\.(?:js|css|json|map|woff2?|ttf|eot|otf|html?|php|xml)(?:$|[?#])", re.I
)


# ------------------------------------------------------------------
# 网页解码：中文页面的编码必须谨慎处理
# ------------------------------------------------------------------
# 踩过的坑：requests 的 apparent_encoding（chardet/charset_normalizer）会把
# 中文 UTF-8 页面猜成西里尔编码（实测把「阿忠石磨肠粉」猜成 ptcp154、
# 「喜心斋工夫茶」也猜成 ptcp154），于是商户名变成
# 「й?е©ҶзҹізЈЁиӮ зІү」这种乱码。服务器其实已经明确声明了 charset=utf-8，
# 所以现在一律：HTTP 头 -> <meta charset> -> utf-8 -> gb18030，绝不用 apparent_encoding。
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I
)
_USELESS_CHARSET_RE = re.compile(
    r"^(?:iso-?8859-?1|latin-?1|ascii|us-ascii|cp1252|windows-1252|none)$", re.I
)
_CYRILLIC_RE = re.compile(r"[\u0400-\u052F]")
_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")


def looks_mojibake(text):
    """判断一段文字是不是「中文被当成西里尔/其它编码解开」的乱码。"""
    if not text:
        return False
    sample = str(text)[:500]
    cyrillic = len(_CYRILLIC_RE.findall(sample))
    cjk = len(_CJK_RE.findall(sample))
    return cyrillic >= 3 and cyrillic > cjk


def decode_html_bytes(raw, declared_encoding=""):
    """
    按 HTTP 头 charset -> <meta charset> -> utf-8 -> gb18030 -> big5 的顺序解码，
    返回 (文本, 实际使用的编码)。ISO-8859-1 这类「等于没声明」的值会被忽略
    （requests 在没有 charset 时就会填它）。
    """
    if not raw:
        return "", "utf-8"
    candidates = []
    declared = str(declared_encoding or "").strip()
    if declared and not _USELESS_CHARSET_RE.match(declared):
        candidates.append(declared)
    meta = _META_CHARSET_RE.search(raw[:4096])
    if meta:
        try:
            candidates.append(meta.group(1).decode("ascii", "ignore"))
        except Exception:
            pass
    candidates.extend(["utf-8", "gb18030", "big5"])

    fallback = ("", "utf-8")
    for encoding in candidates:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if not looks_mojibake(text):
            return text, encoding
        if not fallback[0]:
            fallback = (text, encoding)
    if fallback[0]:
        return fallback
    return raw.decode("utf-8", "replace"), "utf-8"


def image_basename(url):
    """
    取图片文件名（去掉 @尺寸 后缀），用于「同图不同尺寸后缀」的兜底匹配。

    相册卡片里是 ...558858.jpg%40240w_180h...，而页面脚本里可能出现
    ...558858.jpg@600w_600h... 或原图 ...558858.jpg；三者 basename 相同，
    因此即使 URL 变体对不上，也能把卡片元数据绑定到正确的图片。
    """
    if not url:
        return ""
    try:
        path = urlparse(normalize_url(url)).path or ""
    except Exception:
        path = str(url)
    name = unescape(path.rsplit("/", 1)[-1])
    name = _SIZE_SUFFIX_RE.sub("", name)
    return name.strip().lower()


def original_size_image_url(url):
    """
    把缩略图 URL 还原成原图 URL（尽力而为，失败时调用方会回退到原 URL）。

    实测：https://img.meituan.net/ugcpic/xxx.jpg%40240w_180h_1e_1c_1l%7Cwatermark%3D0
          -> 240x180 缩略图；去掉后缀后为 1280x853 原图。
    """
    if not url:
        return url
    try:
        m = _SIZE_SUFFIX_RE.search(url)
        if m:
            query_at = url.find("?")
            if query_at == -1 or m.start() < query_at:
                return url[:m.start()]
        if _TRANSFORM_QUERY_RE.search(url):
            return url.split("?", 1)[0]
    except Exception:
        pass
    return url


# 相册页 <script> 内嵌的照片数据（2026-09 实测）：
#   'records': '{"img":[{"full":"<原图>","thumb":"<小图>","picId":7605587866}, ...]}'
# picId 与相册卡片的 /photos/<id> 完全一致（实测 3 家商户 16/16 完全对应），
# 因此可以用它把「原图」绑定到具体照片。
_PHOTO_RECORD_RE = re.compile(
    r'\{"full":"([^"]+?)","thumb":"([^"]+?)","picId":(\d+)\}'
)


def image_download_candidates(page_url, photo_id="", assets=None):
    """
    按画质从高到低给出可尝试的下载地址（均为实测结论）：

      1. 去掉 @尺寸 后缀的地址 —— 美团 CDN：240x180 -> 1280x853
      2. 页面内嵌 JSON 的 full —— 点评 CDN：240x180 -> 700x525
      3. 相册卡片里的原始地址（兜底）

    实测相册卡片里的 <img src> 只有 240x180，低于默认的「最小边长 200」过滤阈值，
    因此不换地址的话这些图片会被整批当成"过小"跳过。
    """
    candidates = []

    def add(url):
        if url and url not in candidates:
            candidates.append(url)

    stripped = original_size_image_url(page_url)
    if stripped and stripped != page_url:
        add(stripped)

    if assets and photo_id:
        record = assets.get(str(photo_id))
        if isinstance(record, dict):
            full = unescape(record.get("full") or "").replace("\\/", "/").strip()
            if full and not full.startswith("http"):
                full = "https:" + full if full.startswith("//") else ""
            if full:
                add(normalize_url(full))

    add(page_url)
    return candidates


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def calculate_phash(data):
    try:
        with Image.open(BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            return str(imagehash.phash(img))
    except Exception:
        return None


def image_dimensions(data):
    try:
        with Image.open(BytesIO(data)) as img:
            return img.width, img.height
    except Exception:
        return 0, 0


def is_probably_image_url(url):
    if not url:
        return False
    lower = url.lower()
    if re.search(r"\.(jpg|jpeg|png|webp|gif|bmp|avif)(?:$|[?#])", lower):
        return True
    return any(
        x in lower
        for x in ("image", "img", "photo", "pic", "album", "upload", "cdn")
    )


def page_url(shop_id, page):
    return f"https://www.dianping.com/shop/{shop_id}/photos?pg={page}"


DIANPING_URL_RE = re.compile(
    r"https?://(?:m\.)?dianping\.com/"
    r"(?:shopinfo|shopshare|shop)/[A-Za-z0-9_-]+"
    r"(?:\?[^\s]*)?",
    re.I,
)


def extract_dianping_url(text):
    if not text:
        return None
    text = str(text)
    text = text.replace("&amp;", "&").replace("\\&", "&").strip()
    m = DIANPING_URL_RE.search(text)
    if m:
        return m.group(0)
    if re.match(r"^https?://", text, re.I):
        return text
    return None


def photos_count_to_last_page(count, photos_per_page=16):
    count = int(count)
    if count <= 0:
        raise ValueError("图片数量必须大于 0")
    return (count + photos_per_page - 1) // photos_per_page


# ============================================================
# 发布时间解析
# ============================================================
#
# 大众点评相册卡片里的时间只有三种形态（2026-09 实测 5 家商户 160 张）：
#   25-12-18  -> 跨年，带 2 位年份        -> 2025-12-18
#   06-21     -> 当年，不带年份（65/160）  -> 2026-06-21（按「当年显示 MM-DD」推定）
#   2019-09-24 / 2019年9月24日             -> 全格式
# 另外再兼容 今天/昨天/N天前 这类相对时间（评价页会出现）。

_FULL_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
    r"(?:\s*[T\s]\s*(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?)?(?!\d)"
)
_SHORT_YEAR_DATE_RE = re.compile(
    r"(?<!\d)(\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})"
    r"(?:\s*[T\s]\s*(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?)?(?!\d)"
)
_MONTH_DAY_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:[-/.]|月)\s*(\d{1,2})\s*日?"
    r"(?:\s*[T\s]\s*(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?)?(?!\d)"
)
_RELATIVE_DATE_RE = re.compile(
    r"(今天|昨天|前天|\d+\s*(?:分钟|小时|天|周|个?月|年)前)"
)

_SHORT_DATE_OUTPUT_RE = re.compile(r"^\d{1,2}-\d{1,2}(?: \d{1,2}:\d{2}(?::\d{2})?)?$")


def _format_published(year, month, day, hour=None, minute=None, second=None):
    """拼成 YYYY-MM-DD[ HH:MM[:SS]]；非法月日返回空串。"""
    try:
        month = int(month)
        day = int(day)
    except (TypeError, ValueError):
        return ""
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    text = f"{int(year):04d}-{month:02d}-{day:02d}"
    if hour in (None, ""):
        return text
    try:
        hour = int(hour)
        minute = int(minute or 0)
    except (TypeError, ValueError):
        return text
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return text
    text += f" {hour:02d}:{minute:02d}"
    if second not in (None, ""):
        try:
            second = int(second)
        except (TypeError, ValueError):
            return text
        if 0 <= second <= 59:
            text += f":{second:02d}"
    return text


def _is_part_of_longer_date(text, match):
    """避免把 '25-12-18' 中的 '25-12' 当成 MM-DD。"""
    after = text[match.end():].lstrip()
    if after[:1] in ("-", "/", ".") and after[1:2].isdigit():
        return True
    before = text[:match.start()].rstrip()
    if before[-1:] in ("-", "/", ".") and before[-2:-1].isdigit():
        return True
    return False


def _relative_to_datetime(raw, now):
    raw = re.sub(r"\s+", "", raw or "")
    if raw == "今天":
        return now
    if raw == "昨天":
        return now - timedelta(days=1)
    if raw == "前天":
        return now - timedelta(days=2)
    m = re.match(r"(\d+)(分钟|小时|天|周|个?月|年)前$", raw)
    if not m:
        return None
    amount = int(m.group(1))
    unit = m.group(2)
    if unit == "分钟":
        return now - timedelta(minutes=amount)
    if unit == "小时":
        return now - timedelta(hours=amount)
    if unit == "天":
        return now - timedelta(days=amount)
    if unit == "周":
        return now - timedelta(days=7 * amount)
    if unit in ("月", "个月"):
        return now - timedelta(days=30 * amount)
    return now - timedelta(days=365 * amount)


def parse_published_at(text, now=None, infer_year=True):
    """
    从一段文本里解析发布时间。

    返回 (published_at, published_raw, year_inferred, matched)：
      published_at  : 'YYYY-MM-DD[ HH:MM[:SS]]'；infer_year=False 且页面只有 MM-DD 时
                      保留 'MM-DD' 原样；解析失败返回 ''
      published_raw : 页面上实际出现的片段（如 '06-21'）
      year_inferred : True 表示年份是按「当年显示 MM-DD、跨年显示 YY-MM-DD」推定的
      matched       : 是否识别到日期
    """
    if not text:
        return "", "", False, False

    text = unescape(str(text))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "", "", False, False

    now = now or datetime.now()

    m = _FULL_DATE_RE.search(text)
    if m:
        value = _format_published(
            m.group(1), m.group(2), m.group(3),
            m.group(4), m.group(5), m.group(6),
        )
        if value:
            return value, m.group(0).strip(), False, True

    m = _SHORT_YEAR_DATE_RE.search(text)
    if m:
        year = 2000 + int(m.group(1))
        if year > now.year + 1:
            year = 1900 + int(m.group(1))
        value = _format_published(
            year, m.group(2), m.group(3),
            m.group(4), m.group(5), m.group(6),
        )
        if value:
            return value, m.group(0).strip(), False, True

    m = _MONTH_DAY_RE.search(text)
    if m and not _is_part_of_longer_date(text, m):
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            if infer_year:
                # 实测规则：当年照片只显示 MM-DD，跨年才显示 YY-MM-DD。
                # 极少数情况下出现「晚于今天」的月日，退一步按上一年处理。
                year = now.year - 1 if (month, day) > (now.month, now.day) else now.year
                value = _format_published(
                    year, month, day, m.group(3), m.group(4), m.group(5)
                )
                if value:
                    return value, m.group(0).strip(), True, True
            else:
                value = f"{month:02d}-{day:02d}"
                if m.group(3) not in (None, ""):
                    try:
                        value += f" {int(m.group(3)):02d}:{int(m.group(4) or 0):02d}"
                    except (TypeError, ValueError):
                        pass
                return value, m.group(0).strip(), False, True

    m = _RELATIVE_DATE_RE.search(text)
    if m:
        moment = _relative_to_datetime(m.group(0), now)
        if moment is not None:
            return moment.strftime("%Y-%m-%d %H:%M"), m.group(0), False, True

    return "", "", False, False


def is_short_date(value):
    return bool(value) and bool(_SHORT_DATE_OUTPUT_RE.match(str(value)))


# 页面上明显不是用户名的文本（相册导航、按钮、报错链接等）
_NON_UPLOADER_RE = re.compile(
    r"^(?:全部图片|上传图片|商户官方图片|官方图片|报错|举报|查看更多|查看商户|"
    r"点击看大图|展开|收起|关闭|上一页|下一页|全部|图片|点评|赞|收藏|分享)$"
)
_NON_UPLOADER_TAIL_RE = re.compile(
    r"(?:上传图片|商户官方图片|官方图片|全部图片|点击看大图)$"
)


def clean_uploader(value, max_len=40):
    """清洗上传者文本；明显不是用户名的返回空串（宁可为空也不要错值）。"""
    if not value:
        return ""
    text = unescape(str(value))
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("|｜·•:：,，、-— \t\r\n")
    if not text or len(text) > max_len:
        return ""
    if _NON_UPLOADER_RE.match(text) or _NON_UPLOADER_TAIL_RE.search(text):
        return ""
    if re.fullmatch(r"[\d\s\-/.]+", text):  # 纯数字/日期不是用户名
        return ""
    if not re.search(r"[^\W\d_]", text, re.UNICODE):  # 必须含字母或汉字
        return ""
    return text


def clean_display_name(value, max_len=40):
    """
    清洗「确定的」用户名：来自 /member/<uid> 链接或 $PicReport(...) 的第三参数，
    语义上就是上传者，所以只做去空白和长度限制，不做启发式丢弃
    （用户名可能是 "W."、纯 emoji 或含空格）。
    """
    if not value:
        return ""
    text = unescape(str(value))
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("|｜")
    if not text or len(text) > max_len:
        return ""
    return text


def metadata_fields(metadata):
    """
    把 metadata_map 里的一条记录展开成 add_image / update_image_* 的参数形式，
    避免在各处手写字段名导致漏字段。
    """
    metadata = metadata or {}
    try:
        photo_index = int(metadata.get("photo_index", -1))
    except (TypeError, ValueError):
        photo_index = -1
    try:
        confidence = int(metadata.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0
    return {
        "uploader": metadata.get("uploader", "") or "",
        "published_at": metadata.get("published_at", "") or "",
        "metadata_source": metadata.get("metadata_source", "") or "",
        "published_raw": metadata.get("published_raw", "") or "",
        "year_inferred": 1 if metadata.get("year_inferred") else 0,
        "metadata_confidence": confidence,
        "photo_id": metadata.get("photo_id", "") or "",
        "photo_index": photo_index,
    }


# ============================================================
# 商户信息 / 日期范围 / 店铺 ID
# ============================================================

def _title_field(html, tag="title"):
    match = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", html or "", re.I)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def parse_shop_meta_from_album(html):
    """
    从相册页解析商户名称、城市、照片总数（实测三种来源）。

      <title>富贵面馆(镇坪路店)-图片-上海-大众点评网</title>
      <meta name="Keywords" content="所有图片,富贵面馆(镇坪路店)" />
      'albumPicCount': 3379
    """
    result = {
        "shop_name": "", "city": "", "photo_count": 0, "shop_id": "",
    }
    if not html:
        return result

    count_match = re.search(r"albumPicCount'?\s*:\s*(\d+)", html)
    if count_match:
        try:
            result["photo_count"] = int(count_match.group(1))
        except ValueError:
            pass

    shop_match = re.search(r"referShopID\"?\s*:\s*\"?([A-Za-z0-9_-]+)", html)
    if shop_match:
        result["shop_id"] = shop_match.group(1)

    title = _title_field(html)
    if title:
        # 商户名里可能带 "-"，所以用 "图片" 做锚点，兼容 "-第N页"
        match = re.match(
            r"^(.+?)-(?:图片|相册)-(.+?)(?:-第\d+页)?(?:-大众点评网)?$", title
        )
        if match:
            result["shop_name"] = match.group(1).strip()
            city = match.group(2).strip()
            if city and city != "大众点评网":
                result["city"] = city

    if not result["shop_name"]:
        keywords = re.search(
            r'name="Keywords"\s+content="([^"]*)"', html, re.I
        )
        if keywords:
            segments = [x.strip() for x in keywords.group(1).split(",")]
            if len(segments) >= 2 and segments[1] not in (
                "所有图片", "图片", "相册"
            ):
                result["shop_name"] = unescape(segments[1])

    if not result["shop_name"]:
        anchor = re.search(
            r'href="/shop/[A-Za-z0-9_-]+"[^>]*>([^<]{2,60})</a>', html
        )
        if anchor:
            result["shop_name"] = unescape(anchor.group(1)).strip()

    return result


def album_last_page(photo_count, photos_per_page=16):
    """相册是「新→旧」整体有序的，末页 = ceil(总数 / 每页数)（实测 5/5 精确）。"""
    try:
        count = int(photo_count)
    except (TypeError, ValueError):
        return None
    if count <= 0:
        return None
    return (count + photos_per_page - 1) // photos_per_page


def normalize_date_bound(value):
    """把用户输入的日期规整成 YYYY-MM-DD；无法识别返回空串。"""
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    match = re.search(
        r"((?:19|20)\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]?\s*(\d{1,2})?\s*日?",
        text,
    )
    if not match:
        # 只写了年份，例如 "2010"
        year_only = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)
        if not year_only:
            return ""
        return f"{int(year_only.group(1)):04d}-01-01"
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    if not (1 <= month <= 12):
        month = 1
    if not (1 <= day <= 31):
        day = 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def date_in_range(value, date_from="", date_to=""):
    """
    判断图片日期是否落在 [date_from, date_to] 内。
    返回 True / False；日期未知（老数据可能是 MM-DD）返回 None。
    """
    day = str(value or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return None
    if date_from and day < date_from:
        return False
    if date_to and day > date_to:
        return False
    return True


def estimate_years(oldest_date, now=None):
    """按「最早的照片」估算商户至少经营了多少年（下界）。"""
    day = str(oldest_date or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return None
    try:
        moment = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None
    now = now or datetime.now()
    days = (now - moment).days
    if days < 0:
        return 0.0
    return round(days / 365.25, 1)


SHOP_ID_RE = re.compile(
    r"(?:dianping\.com)?/(?:shop|shopinfo|shopshare)/([A-Za-z0-9_-]{3,})",
    re.I,
)
_BAD_SHOP_IDS = {
    "photos", "info", "tag", "officialphotos", "upload", "search", "shop",
}
# 风控页的特征（跳到这里说明没登录 / Cookie 失效 / 被判定为爬虫）
VERIFY_PAGE_RE = re.compile(
    r"spiderindefence|verify\.meituan\.com|验证中心|security-check", re.I
)


def is_verify_page(html_or_url):
    """判断是不是被美团风控拦到「验证中心」了。"""
    return bool(VERIFY_PAGE_RE.search(str(html_or_url or "")))


# cmd 版「Copy as cURL」会把特殊字符转义成 ^X：实测 cookie 里的 %25 变成 ^%^25、
# %7C 变成 ^%^7C，UA 里的括号变成 ^(Windows…^) —— 注意 ^ 后面有时跟的是数字，
# 所以规则是：只要文本里出现 cmd 转义特征，就把 ^ 全部去掉。
_CMD_MARKER_RE = re.compile(r"\^(?:%|[()|&<>\"!,;=])")


def unescape_cmd_carets(text):
    """去掉 cmd 版 cURL 粘贴带来的 ^ 转义（不是 cmd 文本则原样返回）。"""
    value = str(text or "")
    if not _CMD_MARKER_RE.search(value):
        return value
    return value.replace("^", "")


def parse_curl_command(text):
    """
    从浏览器「Copy as cURL」（Edge/Chrome 右键 → 复制 → 以 cURL 格式复制）里
    提取 URL 与请求头，返回 {"url": ..., "headers": {...}, "cookie": ..., "user_agent": ...}。

    这样用户只要在已登录的浏览器里复制一次，脚本就能带着同一个登录态去抓列表页。
    bash 与 cmd 两种续行符都会先被拉平。
    """
    result = {"url": "", "headers": {}, "cookie": "", "user_agent": ""}
    if not text:
        return result

    cleaned = str(text)
    cleaned = re.sub(r"\\\r?\n", " ", cleaned)      # bash: 行尾 \
    cleaned = re.sub(r"\^\r?\n", " ", cleaned)      # cmd:  行尾 ^
    cleaned = re.sub(r"[\r\n]+", " ", cleaned)
    cleaned = cleaned.replace('^"', '"').replace("^'", "'")  # cmd 常见的 ^" 转义写法
    cleaned = unescape_cmd_carets(cleaned)          # cmd 的 ^%^25 / ^( 之类

    quoted = re.findall(r"(['\"])([^'\"]+)\1", cleaned)
    for _quote, value in quoted:
        if re.match(r"^(?:https?://|/)\S*$", value):
            result["url"] = value
            break
    if not result["url"]:
        bare = re.search(r"\b(https?://[^\s'\"]+)", cleaned)
        if bare:
            result["url"] = bare.group(1)

    for _quote, raw in re.findall(r"(?:-H|--header)\s+(['\"])(.*?)\1", cleaned):
        if ":" not in raw:
            continue
        name, value = raw.split(":", 1)
        result["headers"][name.strip().lower()] = value.strip()
    for _quote, raw in re.findall(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", cleaned):
        result["headers"].setdefault("cookie", raw.strip())
    for _quote, raw in re.findall(
        r"(?:-A|--user-agent)\s+(['\"])(.*?)\1", cleaned
    ):
        result["headers"]["user-agent"] = raw.strip()

    result["cookie"] = result["headers"].get("cookie", "")
    result["user_agent"] = result["headers"].get("user-agent", "")
    return result


def strip_page_marker(url):
    """
    去掉地址里的页码，得到「第 1 页」的地址。

    三类页面的页码写法都不一样（2026-09 实测，都是读页面自己的链接得出的）：
      列表页·只有一级分类  /shanghai/ch10/p2        斜杠式
      列表页·带筛选段      /shanghai/ch10/o11p2     紧贴式（写 /o11/p2 会被 403）
      搜索页               /search/keyword/1/0_x/p2  尾部斜杠式（中间那个 1 不是页码！）
      相册页               /shop/<id>/photos?pg=2    查询参数式
    """
    text = str(url or "").strip()
    if not text:
        return ""
    text = re.sub(r"/p\d+(?=$|[?#])", "", text, flags=re.I)      # 斜杠式
    text = re.sub(r"([?&])pg=\d+", r"\1", text)                  # 查询参数式
    text = re.sub(r"[?&]+(?=$|#)", "", text)

    head, sep, tail = text.partition("?")
    head = head.rstrip("/")
    parts = head.split("/")
    last = parts[-1] if parts else ""
    if re.search(r"p\d+$", last, re.I):
        # 紧贴式：只在「去掉 pN 后确实是筛选代码」时才动手，避免误伤搜索关键词
        candidate = re.sub(r"p\d+$", "", last, flags=re.I)
        if re.fullmatch(
            r"(?:ch|g|r|o|p)\d+(?:(?:ch|g|r|o|p)\d+)*", candidate, re.I
        ):
            parts[-1] = candidate
            head = "/".join(parts)
    return f"{head}?{tail}" if sep else head


def list_page_url(base_url, page):
    """
    把列表页 / 搜索页 / 相册页地址改写成第 N 页的地址。

    实测（带登录 Cookie，上海餐饮为例）：

      /shanghai/ch10/p2         200   只有一级分类时用「斜杠式」
      /shanghai/ch10/o11p2      200   有排序/二级分类/商圈时页码必须「紧贴」上一段
      /shanghai/ch10/g101p2     200   同上
      /shanghai/ch10/r812p2     200   同上
      /shanghai/ch10/g116o11p2  200   组合筛选同理
      /shanghai/ch10/o11/p2     403   ← 用户遇到的 403 就是这个写法造成的
      /shanghai/ch10?pg=2       200   但内容仍然是第 1 页（?pg= 被忽略，绝不能用）
      /search/keyword/1/0_x/p2  200   搜索页用尾部 /pN（中间那个数字不是页码）
      /shop/<id>/photos?pg=2    200   相册页用 ?pg=N

    更稳妥的做法是直接用页面自己给出的「下一页」链接（find_next_page_url），
    这里拼出来的地址只作为兜底。
    """
    url = strip_page_marker(base_url)
    if not url:
        return ""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    if page <= 1:
        return url

    head, sep, tail = url.partition("?")

    # 相册页：页码是查询参数 ?pg=N，不能套用列表页的规则
    if re.search(r"/shop/[A-Za-z0-9_-]+/photos", url, re.I):
        params = [
            item for item in tail.split("&")
            if item and not re.match(r"pg=\d+$", item, re.I)
        ]
        params.append(f"pg={page}")
        return f"{head.rstrip('/')}?{'&'.join(params)}"

    head = head.rstrip("/")
    path = urlparse(head).path.rstrip("/")
    last = path.rsplit("/", 1)[-1] if path else ""
    if re.fullmatch(r"ch\d+", last, re.I):
        merged = f"{head}/p{page}"                 # 只有一级分类：斜杠式
    elif "/search/" not in path.lower() and re.search(
        r"(?:ch|g|r|o|p)\d+$", last, re.I
    ):
        merged = f"{head}p{page}"                  # 有筛选段：紧贴式 o11 -> o11p2
    else:
        merged = f"{head}/p{page}"                 # 搜索页等：尾部斜杠式
    return f"{merged}?{tail}" if sep else merged


def _stdout_can_encode(text):
    """当前控制台编码能不能打印这段文字（Windows 中文控制台默认 GBK）。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return True
    except Exception:
        return False


def console_symbol(ok):
    """命令行里的对勾/叉号。GBK 控制台打不出 ✓/✗ 会直接抛 UnicodeEncodeError，所以先探一下。"""
    return ("✓" if ok else "✗") if _stdout_can_encode("✓") else ("[OK]" if ok else "[X]")


def make_console_encoding_safe():
    """
    让命令行输出在 GBK 控制台下也不崩。

    中文 Windows 的 cmd 默认是 GBK：打印 ✓/✗ 或生僻字会抛 UnicodeEncodeError
    并把整个 --selftest 打断。这里把错误处理改成 replace（不换编码，
    免得中文变成乱码），这样最多是把打不出的字符显示成 ?，
    而不是让程序崩掉。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def safe_print(*parts, **kwargs):
    """print 的兜底版本：编码打不出来时退回 replace，不抛异常。"""
    try:
        print(*parts, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(p) for p in parts)
        stream = kwargs.get("file") or sys.stdout
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            print(text.encode(encoding, "replace").decode(encoding, "replace"), **kwargs)
        except Exception:
            pass


_ANCHOR_TAG_RE = re.compile(r"<a\b([^>]*)>([\s\S]{0,60}?)</a>", re.I)
_HREF_ATTR_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_CLASS_ATTR_RE = re.compile(r"""class\s*=\s*["']([^"']*)["']""", re.I)


def find_next_page_url(html, base_url=""):
    """
    从列表页 HTML 里读点评自己写的「下一页」链接。

    这是最可靠的翻页方式：地址形式（/p2 还是 o11p2）由点评自己决定，
    我们照抄就行，不用猜。找不到就返回空字符串。

    判定条件：`<a>` 的 class 里有 next / title 或文字是「下一页」，
    并且 href 里确实带 p{数字}（避免误抓「下一张图」之类的链接）。
    """
    if not html:
        return ""
    for attrs, text in _ANCHOR_TAG_RE.findall(html):
        href_match = _HREF_ATTR_RE.search(attrs)
        if not href_match:
            continue
        href = unescape(href_match.group(1)).strip()
        if not href or href.lower().startswith(("javascript:", "#", "mailto:")):
            continue
        class_match = _CLASS_ATTR_RE.search(attrs)
        classes = (class_match.group(1) if class_match else "").lower().split()
        label = re.sub(r"<[^>]+>", "", text or "")
        label = re.sub(r"\s+", "", unescape(label))
        is_next = (
            "next" in classes
            or bool(re.search(r"""title\s*=\s*["']下一[页頁]""", attrs, re.I))
            or label in ("下一页", "下一頁", "下页", "下頁")
        )
        if not is_next:
            continue
        if not re.search(r"p\d+", href, re.I):
            continue
        return urljoin(base_url or "https://www.dianping.com", href)
    return ""


# ============================================================
# 列表页地址语法
# ============================================================
#
# 点评的列表页地址是这样拼的：
#
#     https://www.dianping.com/{城市拼音}/ch{一级分类}[/{筛选段}]
#
# 筛选段之间要拼在一起（g 二级分类 / r 商圈 / o 排序 / p 人均）：
#     /shanghai/ch10/o11        200   单个筛选段用斜杠
#     /shanghai/ch10/g116o2     200   多个筛选段拼成一段
#     /shanghai/ch10/g116/o2    404   ← 用斜杠分开多个筛选段是错的
#
# 翻页（2026-09 实测，带登录态，必看）：
#     只有一级分类时         /shanghai/ch10/p2        200
#     带排序/二级/商圈时     /shanghai/ch10/o11p2     200   ← 页码紧贴上一段
#     同样的地址写成斜杠式   /shanghai/ch10/o11/p2    403   ← 别这么写
#     ?pg=2                  返回 200 但内容是第 1 页   ← 会被忽略，别这么写
# 所以翻页一律用 find_next_page_url() 读页面自带的「下一页」链接，
# 兜底才用 list_page_url() 按上面的规则拼。
#
# 实例（用户提供 + 实测）：
#     /shanghai/ch10        -> 上海 · 餐饮（美食）
#     /shanghai/ch10/o11    -> 上海 · 餐饮 · 按「评价最多」排序
#     /shanghai/ch10/g110   -> 餐饮下的二级分类（如火锅）
#     /shanghai/ch10/r883   -> 餐饮 + 某商圈
#     /shanghai/ch10/g116o11 -> 西餐 + 评价最多（组合筛选）
#
# 各段代码的含义不要写死：页面顶部的筛选栏里就有「名称 -> 链接」的完整对应关系，
# 所以这个工具的做法是「从页面里学」，见 learn_list_filters()。

# 常用城市拼音（首次使用时给个像样的默认值；城市表同样会从页面里学到）
CITY_PINYIN = {
    "北京": "beijing", "上海": "shanghai", "广州": "guangzhou", "深圳": "shenzhen",
    "天津": "tianjin", "重庆": "chongqing", "杭州": "hangzhou", "南京": "nanjing",
    "苏州": "suzhou", "无锡": "wuxi", "宁波": "ningbo", "温州": "wenzhou",
    "绍兴": "shaoxing", "嘉兴": "jiaxing", "金华": "jinhua", "台州": "taizhou",
    "合肥": "hefei", "福州": "fuzhou", "厦门": "xiamen", "泉州": "quanzhou",
    "南昌": "nanchang", "济南": "jinan", "青岛": "qingdao", "烟台": "yantai",
    "潍坊": "weifang", "淄博": "zibo", "郑州": "zhengzhou", "洛阳": "luoyang",
    "武汉": "wuhan", "长沙": "changsha", "株洲": "zhuzhou", "常德": "changde",
    "东莞": "dongguan", "佛山": "foshan", "珠海": "zhuhai", "中山": "zhongshan",
    "惠州": "huizhou", "汕头": "shantou", "潮州": "chaozhou", "揭阳": "jieyang",
    "南宁": "nanning", "桂林": "guilin", "海口": "haikou", "三亚": "sanya",
    "成都": "chengdu", "绵阳": "mianyang", "贵阳": "guiyang", "昆明": "kunming",
    "大理": "dali", "丽江": "lijiang", "西安": "xian", "咸阳": "xianyang",
    "兰州": "lanzhou", "西宁": "xining", "银川": "yinchuan", "太原": "taiyuan",
    "石家庄": "shijiazhuang", "唐山": "tangshan", "保定": "baoding",
    "沈阳": "shenyang", "大连": "dalian", "长春": "changchun", "吉林": "jilin",
    "哈尔滨": "haerbin", "徐州": "xuzhou", "常州": "changzhou",
    "南通": "nantong", "扬州": "yangzhou", "镇江": "zhenjiang",
}
CITY_NAME_BY_PINYIN = {}
for _cn, _py in CITY_PINYIN.items():
    CITY_NAME_BY_PINYIN.setdefault(_py, _cn)
del _cn, _py

_SEGMENT_KINDS = ("ch", "g", "r", "p", "o")
_SEGMENT_FIELD = {"ch": "category", "g": "group", "r": "region",
                  "p": "price", "o": "sort"}


def city_pinyin(name):
    """「上海」/「shanghai」都能得到 'shanghai'；城市表里没有的按拼音原样返回。"""
    text = str(name or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"[A-Za-z]{2,24}", text):
        return text.lower()
    return CITY_PINYIN.get(text, "")


def city_label(pinyin):
    return CITY_NAME_BY_PINYIN.get(str(pinyin or "").strip().lower(), "")


def parse_list_url(url):
    """
    解析列表页地址。返回：
      city     城市拼音（如 shanghai，搜索页可能是空）
      category/group/region/price/sort  各筛选段代码（如 ch10 / g110 / o11）
      page     页码。三种写法都认：斜杠式 /p2、紧贴式（o11p2 / g101p2，
               带排序/二级分类/商圈时点评要求这么写）、以及 ?pg=N
               （但注意点评服务器会忽略 ?pg=，它只是被解析出来用于改写地址）
      segments 原始顺序的 [(kind, id)]
    """
    result = {
        "url": str(url or ""), "city": "", "category": "", "group": "",
        "region": "", "price": "", "sort": "", "page": 1, "segments": [],
        "is_search": False,
    }
    if not str(url or "").strip():
        return result

    try:
        parsed = urlparse(normalize_url(url))
    except Exception:
        return result

    components = [c for c in (parsed.path or "").split("/") if c]
    if components and components[0].lower() == "search":
        result["is_search"] = True
        return result
    if components:
        result["city"] = components[0].lower()

    rest = components[1:]
    if rest:
        last = rest[-1]
        if re.fullmatch(r"p\d+", last, re.I):
            # 斜杠式页码：/shanghai/ch10/p2
            result["page"] = int(last[1:])
            rest = rest[:-1]
        else:
            # 紧贴式页码：/shanghai/ch10/o11p2、/ch10/g101p2、/ch10/g116o2p3
            # （注意 `p{n}` 本身也是「人均」价格段的代码，所以只有紧跟在
            #   ch/g/r/o 代码后面的 p{n} 才当成页码）
            glued = re.fullmatch(r"(.+?)(p\d+)", last, re.I)
            if glued and re.search(r"(?:ch|g|r|o)\d+$", glued.group(1), re.I):
                result["page"] = int(glued.group(2)[1:])
                rest = rest[:-1] + [glued.group(1)]
    query_page = re.search(r"[?&]pg=(\d+)", str(url))
    if query_page:
        result["page"] = int(query_page.group(1))

    segments = []
    seen = set()
    for component in rest:
        # 兼容 /ch10/g110 与 /ch10g110o11 这两种写法
        for kind, value in re.findall(
            r"(ch|g|r|p|o)(\d+)", component, re.I
        ):
            key = (kind.lower(), value)
            if key in seen:
                continue
            seen.add(key)
            segments.append(key)
    result["segments"] = segments
    for kind, value in segments:
        field = _SEGMENT_FIELD.get(kind)
        if field and not result[field]:
            result[field] = f"{kind}{value}"
    return result


def _normalize_segment(value, prefix):
    """把「美食」之外的写法统一成 ch10 / o11 这种代码：'10'、'ch10' 都接受。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if re.fullmatch(r"\d+", text):
        return f"{prefix}{text}"
    if re.fullmatch(rf"{prefix}\d+", text):
        return text
    return ""


def build_list_url(city, category="", group="", region="", price="",
                   sort="", page=1):
    """
    拼出列表页地址：/{城市}/ch{分类}[/{筛选段…}][页码]

    筛选段之间要「拼在一起」：/ch10/o11、/ch10/g116o2；
    （实测 /ch10/g116/o2 会 404，所以不能一段一段用斜杠分开。）

    例：build_list_url("上海", category="ch10", sort="o11")
        -> https://www.dianping.com/shanghai/ch10/o11
        build_list_url("上海", category="ch10", sort="o11", page=2)
        -> https://www.dianping.com/shanghai/ch10/o11p2   （不能写成 /o11/p2，会被 403）
    """
    pinyin = city_pinyin(city)
    if not pinyin:
        return ""
    codes = [
        _normalize_segment(category, "ch"),
        _normalize_segment(group, "g"),
        _normalize_segment(region, "r"),
        _normalize_segment(price, "p"),
        _normalize_segment(sort, "o"),
    ]
    codes = [c for c in codes if c]
    if not codes or not codes[0].startswith("ch"):
        # 点评列表页一定是 /{城市}/ch{分类}/…，没给分类时补默认的美食
        codes.insert(0, "ch10")
    category_code, filters = codes[0], codes[1:]
    url = f"https://www.dianping.com/{pinyin}/{category_code}"
    if filters:
        # 多个筛选段必须「拼在一起」成一段（/ch10/g116o2）。
        # 实测 /ch10/g116/o2 是 404，只有单个筛选段才可以写成 /ch10/o11。
        url += "/" + "".join(filters)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    if page > 1:
        # 页码写法由 list_page_url 统一负责（带筛选段时要紧贴上一段）
        url = list_page_url(url, page)
    return url


_ANCHOR_RE = re.compile(
    r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]{0,120}?)</a>""",
    re.I,
)


def _clean_anchor_text(raw):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("»>·|-— ")
    text = re.sub(r"\(\d+\)$", "", text).strip()
    return text[:40]


def learn_list_filters(html, base_url=""):
    """
    从列表页 HTML 里学习「名称 -> 代码/链接」的对应关系（页面上写什么就学什么）。

    返回（都是有序列表，可直接存进设置或喂给界面下拉框）：
      cities     [{label, pinyin}]
      categories [{label, code, url}]   一级分类，如 美食 -> ch10
      groups     [{label, code, url}]   二级分类，如 火锅 -> g110
      regions    [{label, code, url}]   商圈
      sorts      [{label, code, url}]   排序，如 评价最多 -> o11
    """
    learned = {"cities": [], "categories": [], "groups": [],
               "regions": [], "sorts": []}
    if not html:
        return learned
    base_pinyin = parse_list_url(base_url)["city"] if base_url else ""
    seen = {key: set() for key in learned}

    for href, raw_text in _ANCHOR_RE.findall(html):
        href = unescape(href or "").strip()
        if not href or href.lower().startswith(("javascript", "#")):
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://www.dianping.com" + href
        if "dianping.com" not in href:
            continue
        href = href.split("#")[0]

        info = parse_list_url(href)
        label = _clean_anchor_text(raw_text)

        # 城市切换链接：只有一段城市拼音
        if info["city"] and not info["segments"] and label:
            if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", label):
                if info["city"] not in seen["cities"]:
                    seen["cities"].add(info["city"])
                    learned["cities"].append(
                        {"label": label, "pinyin": info["city"]}
                    )
            continue
        if not info["city"] or not info["segments"] or not label:
            continue
        if base_pinyin and info["city"] != base_pinyin:
            continue

        if info["category"] and info["sort"] and not info["group"] \
                and not info["region"]:
            bucket, code = "sorts", info["sort"]
        elif info["category"] and not info["sort"] and not info["group"] \
                and not info["region"]:
            bucket, code = "categories", info["category"]
        elif info["group"]:
            bucket, code = "groups", info["group"]
        elif info["region"]:
            bucket, code = "regions", info["region"]
        else:
            continue

        key = code
        if key in seen[bucket]:
            continue
        seen[bucket].add(key)
        learned[bucket].append({"label": label, "code": code, "url": href})
    return learned


def find_learned_option(learned, kind, value):
    """
    在学到的选项里按「名称 / 代码 / 纯数字」查找，返回该选项 dict（找不到返回 None）。
    例：find_learned_option(learned, "categories", "美食") 或 ("...", "ch10") 或 ("...", "10")
    """
    if not learned or value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for item in learned.get(kind) or []:
        code = str(item.get("code") or "")
        digits = re.sub(r"^\D+", "", code)
        if text == item.get("label") or text.lower() == code.lower() \
                or text == digits:
            return item
    return None


def extract_shop_ids(text):
    """
    从任意文本 / HTML（列表页、搜索结果页、分享文本）里提取店铺 ID，去重且保持顺序。

    点评的分类列表页和搜索页都有风控（会跳到验证中心），所以实际用法是：
    用浏览器打开这些页面后「另存为」HTML，再交给脚本解析。
    """
    if not text:
        return []
    result = []
    seen = set()
    for match in SHOP_ID_RE.finditer(str(text)):
        shop_id = match.group(1)
        if shop_id.lower() in _BAD_SHOP_IDS or shop_id in seen:
            continue
        seen.add(shop_id)
        result.append(shop_id)
    return result


# ============================================================
# HTTP
# ============================================================

class HTTPClient:

    def __init__(self, settings):
        self.settings = settings
        self.rate_limit_hits = 0        # 被 403/429/503 拦了几次（用于提醒降速）
        self.cookie_dropped_for_shop = False
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,image/avif,"
                    "image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
            }
        )
        self.apply_login_state()

    def apply_login_state(self, settings=None):
        """
        把设置里的 Cookie / User-Agent 应用到会话上。

        点评的列表页、搜索页、商户主页都有风控，只有带上登录态才能访问；
        相册页不需要，所以没填 Cookie 时一切照旧。
        """
        settings = settings or self.settings
        cookie = unescape_cmd_carets(
            str(settings.get("cookie", "") or "").strip()
        )
        if cookie and "\n" in cookie:
            cookie = " ".join(cookie.split())
        if cookie:
            self.session.headers["Cookie"] = cookie
        else:
            self.session.headers.pop("Cookie", None)

        user_agent = unescape_cmd_carets(
            str(settings.get("user_agent", "") or "").strip()
        )
        if user_agent:
            self.session.headers["User-Agent"] = user_agent
        else:
            self.session.headers["User-Agent"] = USER_AGENT

        referer = str(settings.get("list_url", "") or "").strip()
        if referer.startswith("http"):
            self.session.headers["Referer"] = referer

    @property
    def has_cookie(self):
        return bool(self.session.headers.get("Cookie"))

    # ---------- 请求 ----------

    _SHOP_URL_RE = re.compile(r"/(?:shop|shopinfo|shopshare)/", re.I)

    def _should_use_cookie(self, url):
        """
        相册页 / 商户页不带登录态（本来就不需要，带上反而可能被更严地对待）；
        列表页、搜索页等有风控的页面才带。
        """
        return not self._SHOP_URL_RE.search(str(url or ""))

    def _request(self, url, timeout, use_cookie, **kwargs):
        """按需临时切换 Cookie 发一次请求，结束后恢复会话状态。"""
        cookie = str(self.settings.get("cookie", "") or "").strip()
        saved = self.session.headers.pop("Cookie", None)
        if use_cookie and cookie:
            self.session.headers["Cookie"] = cookie
        try:
            return self.session.get(
                url, timeout=timeout, allow_redirects=True, **kwargs
            )
        finally:
            if saved is not None:
                self.session.headers["Cookie"] = saved
            elif cookie:
                self.session.headers["Cookie"] = cookie
            else:
                self.session.headers.pop("Cookie", None)

    def get_html(self, url, with_cookie=None, retries=2):
        """
        抓 HTML。返回 requests.Response 或 None。

        * 解码：HTTP 头 charset -> <meta charset> -> utf-8 -> gb18030（见 decode_html_bytes）
        * 403/429/503：退避重试；如果带着登录态被拦，会自动改用无登录态再试一次
          （相册页本来就不需要登录态，实测带 Cookie 反而更容易被 WAF 拦）
        * with_cookie=None 表示自动判断（相册页不带、列表/搜索页带）
        """
        timeout = self.settings.get("request_timeout", 15)
        use_cookie = (
            self._should_use_cookie(url) if with_cookie is None
            else bool(with_cookie)
        )
        attempt = 0
        last_response = None
        while attempt <= retries:
            attempt += 1
            try:
                response = self._request(url, timeout, use_cookie)
            except Exception:
                if attempt <= retries:
                    time.sleep(min(8.0, 1.5 * attempt))
                    continue
                return None

            response.encoding = None  # 关键：不要用 apparent_encoding 去猜中文页面
            declared = ""
            content_type = response.headers.get("Content-Type", "") or ""
            charset_match = re.search(r"charset=([\w\-]+)", content_type, re.I)
            if charset_match:
                declared = charset_match.group(1)
            text, used_encoding = decode_html_bytes(
                response.content or b"", declared
            )
            response.encoding = used_encoding   # 让 response.text 与解码结果一致
            response._decoded_text = text

            last_response = response
            code = response.status_code
            if code in (403, 429, 503):
                self.rate_limit_hits += 1
                if use_cookie and with_cookie is None:
                    # 带登录态被拦 -> 换成无登录态重试（相册页不需要登录态）
                    use_cookie = False
                    self.cookie_dropped_for_shop = True
                    continue
                if attempt <= retries:
                    time.sleep(min(10.0, 2.0 * attempt + 1.0))
                    continue
            return response
        return last_response

    def get_html_text(self, url, with_cookie=None, **_kwargs):
        """返回解码后的文本（拿不到就返回空串）。"""
        response = self.get_html(url, with_cookie=with_cookie)
        if response is None:
            return ""
        return getattr(response, "_decoded_text", None) or (response.text or "")

    def get_bytes(self, url, referer=None):
        headers = {}
        if referer:
            headers["Referer"] = referer
        try:
            timeout = self.settings.get("image_timeout", 25)
            r = self.session.get(
                url, timeout=timeout,
                headers=headers, allow_redirects=True,
            )
            if r.status_code in (403, 429, 503):
                self.rate_limit_hits += 1
            if r.status_code != 200:
                return None
            return r.content or None
        except Exception:
            return None

    def get_bytes(self, url, referer=None):
        headers = {}
        if referer:
            headers["Referer"] = referer
        try:
            timeout = self.settings.get("image_timeout", 25)
            r = self.session.get(
                url, timeout=timeout,
                headers=headers, allow_redirects=True,
            )
            if r.status_code != 200:
                return None
            return r.content or None
        except Exception:
            return None


# ============================================================
# URL Resolver
# ============================================================

class DianpingURLResolver:

    SHOP_RE = re.compile(r"/shop/([^/?#]+)", re.I)
    SHARE_RE = re.compile(r"/(?:shopinfo|shopshare)/([^/?#]+)", re.I)

    def __init__(self, http_client):
        self.http = http_client

    def detect_type(self, url):
        url = normalize_url(url)
        path = urlparse(url).path.lower()
        if "/photos" in path:
            return "photos"
        if "/shopinfo" in path:
            return "shopinfo"
        if "/shopshare" in path:
            return "shopshare"
        if "/shop/" in path:
            return "shop"
        return "unknown"

    def extract_shop_id(self, url):
        if not url:
            return None
        m = self.SHOP_RE.search(url)
        return m.group(1) if m else None

    def extract_share_id(self, url):
        if not url:
            return None
        m = self.SHARE_RE.search(url)
        return m.group(1) if m else None

    def resolve(self, url):
        url = normalize_url(url)
        typ = self.detect_type(url)

        shop_id = self.extract_shop_id(url)
        if shop_id:
            return {
                "input_url": url,
                "final_url": url,
                "shop_id": shop_id,
                "type": typ,
            }

        share_id = self.extract_share_id(url)
        if share_id:
            final_url = (
                f"https://www.dianping.com/shop/"
                f"{share_id}/photos"
            )
            return {
                "input_url": url,
                "final_url": final_url,
                "shop_id": share_id,
                "type": typ,
            }

        return {
            "input_url": url,
            "final_url": url,
            "shop_id": None,
            "type": typ,
        }


# ============================================================
# HTML Parser
# ============================================================

class DianpingHTMLParser:

    # <script>/<style> 里的地址不是页面上展示的图片：
    # 点评相册页把每张照片的「原图 + 小图」放在内嵌 JSON 里，旧版会把它们
    # 统统当成独立图片，实测 16 张照片的页面会变成 48 个待下载地址（3 倍流量）。
    STRIP_FOR_URLS_RE = re.compile(
        r"<(script|style)\b[^>]*>[\s\S]*?</\1\s*>", re.I
    )

    def _harvest_urls(self, html):
        urls = []

        attr_pattern = (
            r'(?:src|data-src|data-original|'
            r'data-lazyload|data-lazy-src|data-url|'
            r'data-image)\s*=\s*["\']([^"\']+)'
        )
        for m in re.finditer(attr_pattern, html, flags=re.I):
            urls.append(m.group(1))

        for m in re.finditer(r'srcset\s*=\s*["\']([^"\']+)["\']', html, flags=re.I):
            for item in m.group(1).split(","):
                item = item.strip()
                if not item:
                    continue
                parts = item.split()
                if parts:
                    urls.append(parts[0])

        for m in re.finditer(r'https?://[^\s"\'<>]+', html, flags=re.I):
            candidate = m.group(0).rstrip(".,);]}")
            if is_probably_image_url(candidate):
                urls.append(candidate)

        result = []
        seen = set()
        for url in urls:
            # HTML 实体与转义斜杠统一还原，避免同一张图因写法不同被当成两张
            url = unescape(str(url)).replace("\\/", "/").strip()
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            if not re.match(r"^https?://", url, flags=re.I):
                continue
            url = normalize_url(url)
            if _NON_IMAGE_ASSET_RE.search(url):
                # <script src="....js"> 等资源不是相册图片
                continue
            canonical = canonical_image_url(url)
            if canonical in seen:
                continue
            seen.add(canonical)
            result.append(url)

        return result

    def extract_image_urls(self, html):
        """
        提取页面上真正展示的图片地址。

        先剥掉 <script>/<style> 再扫描，避免把内嵌 JSON 里的「原图/小图变体」
        当成独立图片（原图地址由 extract_photo_assets 单独提取）。
        如果剥掉后一个地址都没有（纯 JS 渲染的版式），退回扫描完整 HTML。
        """
        if not html:
            return []

        stripped = self.STRIP_FOR_URLS_RE.sub(" ", html)
        result = self._harvest_urls(stripped)
        if result:
            return result
        return self._harvest_urls(html)

    def extract_photo_assets(self, html):
        """
        解析页面内嵌的照片 JSON，返回 {picId: {"full": 原图地址, "thumb": 小图地址}}。

        实测 picId 与相册卡片的 /photos/<id> 完全对应，因此可以直接按图片绑定原图。
        """
        assets = {}
        if not html:
            return assets
        for match in _PHOTO_RECORD_RE.finditer(html):
            cleaned = {}
            for key, value in (
                ("full", match.group(1)),
                ("thumb", match.group(2)),
            ):
                value = unescape(value or "").replace("\\/", "/").strip()
                if value.startswith("//"):
                    value = "https:" + value
                if value.startswith("http"):
                    cleaned[key] = normalize_url(value)
            if cleaned:
                assets[match.group(3)] = cleaned
        return assets

    # ------------------------------------------------------------
    # 文本工具
    # ------------------------------------------------------------

    @staticmethod
    def _clean_visible_text(text):
        text = unescape(text or "")
        text = re.sub(r"\s+", " ", text)
        return text.strip(" |\t\r\n")

    @staticmethod
    def _normalize_published_at(value, infer_year=True):
        """兼容旧接口：只返回日期字符串。"""
        published, _raw, _inferred, _matched = parse_published_at(
            value, infer_year=infer_year
        )
        return published

    # ------------------------------------------------------------
    # 相册卡片：定位 -> 逐卡片取「图片 + 作者 + 时间」
    # ------------------------------------------------------------

    # 已知的点评相册卡片容器（按优先级；先命中先返回）
    CARD_SELECTORS = (
        "li.J_list",                     # 现行 PC 相册列表（全部/标签/官方图片）
        "div.J_list",
        "li[class*='photo']",
        "div[class*='photo-item']",
        "div[class*='photoItem']",
        "li[class*='pic']",
        "div[class*='picture-info']",
        ".picture-info",
    )

    # 卡片内肯定不是作者/时间的信息块
    CARD_JUNK_SELECTORS = (
        "script", "style", ".digg-box", ".J_report", ".report", ".hook",
    )

    _PHOTO_HREF_RE = re.compile(r"/photos/(\d+)")
    _MEMBER_HREF_RE = re.compile(r"/member/(\d+)")
    _PIC_REPORT_RE = re.compile(
        r"\$PicReport\(\s*(\d+)\s*,\s*(-?\d+)\s*,\s*'([^']*)'",
        re.I,
    )
    _IMG_URL_ATTRS = (
        "src", "data-src", "data-original", "data-lazyload",
        "data-lazy-src", "data-url", "data-image",
    )

    def __init__(self, infer_year=True):
        self.infer_year = infer_year
        self.last_diagnostics = {}

    # ---------- 图片 URL ----------

    @staticmethod
    def _abs_url(url):
        if not url:
            return ""
        url = unescape(str(url)).replace("\\/", "/").strip()
        if not url:
            return ""
        if url.startswith("//"):
            url = "https:" + url
        if not re.match(r"^https?://", url, re.I):
            return ""
        return normalize_url(url)

    def _img_urls(self, img):
        """取一张 <img> 的所有候选地址（含 srcset/data-* 懒加载属性）。"""
        raw_urls = []
        for attr in self._IMG_URL_ATTRS:
            value = img.get(attr)
            if value:
                raw_urls.append(value)
        srcset = img.get("srcset")
        if srcset:
            for item in str(srcset).split(","):
                item = item.strip()
                if item:
                    raw_urls.append(item.split()[0])

        urls = []
        for raw in raw_urls:
            url = self._abs_url(raw)
            if url and url not in urls:
                urls.append(url)
        return urls

    # ---------- 卡片定位 ----------

    def _find_card_nodes(self, soup):
        """返回相册卡片节点（互不包含，按文档顺序）。"""
        for selector in self.CARD_SELECTORS:
            try:
                found = soup.select(selector)
            except Exception:
                found = []
            nodes = self._promote_and_filter(found)
            if nodes:
                return nodes
        return self._generic_card_nodes(soup)

    def _promote_and_filter(self, found, max_up=4):
        """
        把 .picture-info 这类「只有信息、没有图片」的节点提升到含图片的卡片容器，
        再去掉互相包含的父节点（只保留最内层的卡片）。
        """
        nodes = []
        for node in found:
            if getattr(node, "name", None) in (None, "img"):
                continue
            candidate = node
            for _ in range(max_up):
                if candidate.find("img") is not None:
                    break
                parent = getattr(candidate, "parent", None)
                if parent is None:
                    break
                candidate = parent
            if candidate.find("img") is None:
                continue
            nodes.append(candidate)

        unique = []
        seen = set()
        for node in nodes:
            if id(node) in seen:
                continue
            seen.add(id(node))
            unique.append(node)

        result = []
        for node in unique:
            is_ancestor_of_other = any(
                other is not node and any(p is node for p in other.parents)
                for other in unique
            )
            if is_ancestor_of_other:
                continue
            result.append(node)
        return result

    def _generic_card_nodes(self, soup):
        """
        未知版式兜底：从每张图片向上找「最近的、只含这一张图片、且带作者链接
        或日期」的祖先节点；一旦祖先里出现第 2 张图片就立刻停止。

        旧版（V2.9）反而会给 14 层内的祖先打分并挑最优，多图容器一旦胜出，
        容器里所有图片都会被写上同一份（通常属于最后一张图的）作者/时间。
        """
        cards = []
        seen = set()
        for img in soup.find_all("img"):
            node = img
            for _ in range(12):
                node = node.parent
                if node is None:
                    break
                if len(node.find_all("img")) > 1:
                    break
                text = self._clean_visible_text(node.get_text(" ", strip=True))
                if len(text) > 300:
                    break
                if self._card_member_link(node)[1] or self._card_date(node)[3]:
                    if id(node) not in seen:
                        seen.add(id(node))
                        cards.append(node)
                    break
        return cards

    # ---------- 单卡片信息 ----------

    def _card_member_link(self, card):
        """返回 (用户名, 用户ID)；来自 /member/<uid> 链接，语义确定。"""
        for anchor in card.find_all("a", href=True):
            href = unescape(anchor.get("href", ""))
            match = self._MEMBER_HREF_RE.search(href)
            if not match:
                continue
            name = clean_display_name(anchor.get_text(" ", strip=True))
            if not name:
                name = clean_display_name(anchor.get("title", ""))
            return name, match.group(1)
        return "", ""

    def _card_visible_text(self, card):
        """卡片可见文本，去掉报错/点赞等无关块。"""
        try:
            fresh = BeautifulSoup(str(card), "html.parser")
        except Exception:
            return ""
        try:
            for junk in fresh.select(", ".join(self.CARD_JUNK_SELECTORS)):
                junk.decompose()
        except Exception:
            pass
        return self._clean_visible_text(fresh.get_text(" ", strip=True))

    def _card_date(self, card, card_text=None, infer_year=None):
        """
        返回 (published_at, published_raw, year_inferred, 置信加分, 来源)，
        来源取值：card_sep / card_leaf / card_text / none。
        """
        if infer_year is None:
            infer_year = self.infer_year

        # 1) 结构化组合：<em class="sep">|</em><span>25-12-18</span>
        for separator in card.find_all(["em", "span", "i", "b"]):
            raw_classes = separator.get("class") or []
            if isinstance(raw_classes, str):
                classes = raw_classes.lower()
            else:
                classes = " ".join(raw_classes).lower()
            if "sep" not in classes and "line" not in classes:
                continue
            for sibling in separator.next_siblings:
                if hasattr(sibling, "get_text"):
                    text = self._clean_visible_text(
                        sibling.get_text(" ", strip=True)
                    )
                else:
                    text = self._clean_visible_text(str(sibling))
                if not text:
                    continue
                value, raw, inferred, matched = parse_published_at(
                    text, infer_year=infer_year
                )
                if matched:
                    return value, raw, inferred, 15, "card_sep"

        # 2) 卡片内的短文本叶子节点
        for element in card.find_all(True):
            if getattr(element, "name", "") in ("script", "style"):
                continue
            if element.find(True) is not None:
                continue
            try:
                if element.find_parent(
                    class_=re.compile(r"digg-box|J_report|report|hook", re.I)
                ):
                    continue
            except Exception:
                pass
            text = self._clean_visible_text(element.get_text(" ", strip=True))
            if not text or len(text) > 24:
                continue
            value, raw, inferred, matched = parse_published_at(
                text, infer_year=infer_year
            )
            if matched:
                return value, raw, inferred, 10, "card_leaf"

        # 3) 整张卡片的文本
        if card_text is None:
            card_text = self._card_visible_text(card)
        if card_text and len(card_text) <= 200:
            value, raw, inferred, matched = parse_published_at(
                card_text, infer_year=infer_year
            )
            if matched:
                return value, raw, inferred, 0, "card_text"

        return "", "", False, 0, "none"

    def _card_uploader_guess(self, card, card_text=None):
        """
        没有 /member/ 链接、也没有 $PicReport 时的兜底：
        在卡片可见文本里找「非日期、非页面按钮」的短片段。严格过滤，宁可留空。
        """
        if card_text is None:
            card_text = self._card_visible_text(card)
        if not card_text or len(card_text) > 80:
            return ""
        for segment in re.split(r"[|｜·•]", card_text):
            segment = segment.strip()
            if not segment or len(segment) > 24:
                continue
            if parse_published_at(segment)[3]:
                continue
            name = clean_uploader(segment)
            if name:
                return name
        return ""

    def _card_record(self, card, selector_hit=True, infer_year=None):
        """把一张卡片解析成一条记录。"""
        if infer_year is None:
            infer_year = self.infer_year

        raw_html = str(card)
        record = {
            "photo_id": "", "user_id": "", "uploader": "",
            "published_at": "", "published_raw": "", "year_inferred": False,
            "source": "none", "confidence": 0, "image_urls": [],
        }

        for img in card.find_all("img"):
            for url in self._img_urls(img):
                if url not in record["image_urls"]:
                    record["image_urls"].append(url)

        photo_match = self._PHOTO_HREF_RE.search(raw_html)
        if photo_match:
            record["photo_id"] = photo_match.group(1)
        report_match = self._PIC_REPORT_RE.search(raw_html)
        if report_match:
            if not record["photo_id"]:
                record["photo_id"] = report_match.group(1)
            if report_match.group(2) not in ("", "-1", "0"):
                record["user_id"] = record["user_id"] or report_match.group(2)

        uploader, user_id = self._card_member_link(card)
        if user_id:
            record["user_id"] = user_id
        if uploader:
            record["uploader"] = uploader
        elif report_match and report_match.group(2) not in ("", "-1", "0"):
            # $PicReport(photoId, userId, 'userName')
            record["uploader"] = clean_display_name(report_match.group(3))

        card_text = self._card_visible_text(card)

        if not record["uploader"]:
            record["uploader"] = self._card_uploader_guess(card, card_text)

        published, raw, inferred, _bonus, date_source = self._card_date(
            card, card_text=card_text, infer_year=infer_year
        )
        record["published_at"] = published
        record["published_raw"] = raw
        record["year_inferred"] = bool(inferred)

        if selector_hit:
            confidence = 70
            if date_source in ("card_sep", "card_leaf"):
                confidence += 15
            elif date_source == "card_text":
                confidence += 5
            if record["uploader"]:
                confidence += 15
        else:
            confidence = 45
            if date_source in ("card_sep", "card_leaf"):
                confidence += 10
            if record["uploader"]:
                confidence += 10

        if not record["uploader"] and not record["published_at"]:
            confidence = 0

        record["confidence"] = min(confidence, 100)
        record["source"] = (
            ("card" if selector_hit else "heuristic") + "+" + date_source
        )
        return record

    # ---------- 对外接口 ----------

    @staticmethod
    def _best_meta(candidates):
        if not candidates:
            return None
        best = None
        for meta in candidates:
            if best is None or meta.get("confidence", 0) > best.get(
                "confidence", 0
            ):
                best = meta
        return dict(best) if best else None

    def extract_album_cards(self, html, infer_year=None):
        """解析整页相册卡片，返回记录列表（含每张卡片的图文映射）。"""
        if not html or not BS4_AVAILABLE:
            return []
        if infer_year is None:
            infer_year = self.infer_year
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []
        records = []
        for index, card in enumerate(self._find_card_nodes(soup)):
            try:
                record = self._card_record(card, infer_year=infer_year)
            except Exception:
                continue
            if not record["image_urls"]:
                continue
            record["photo_index"] = index
            records.append(record)
        return records

    def extract_image_metadata(self, html, image_urls, infer_year=None):
        """
        把相册卡片里的「上传者 / 发布时间」绑定到具体图片。

        返回 { canonical_image_url: {...} }，键与 crawl_page 的查询键完全一致。
        绑定顺序：卡片内 URL 精确匹配 -> 去掉 @尺寸 后缀的文件名匹配。
        匹配不到就返回空记录（不再按字符窗口猜测邻居卡片的信息）。
        """
        if infer_year is None:
            infer_year = self.infer_year

        empty = {
            "uploader": "", "user_id": "", "photo_id": "", "photo_index": -1,
            "published_at": "", "published_raw": "", "year_inferred": False,
            "metadata_source": "none", "confidence": 0,
        }

        results = {}
        for url in image_urls or []:
            if not url:
                continue
            results[canonical_image_url(url)] = dict(empty)

        diagnostics = {
            "bs4": BS4_AVAILABLE,
            "error": "",
            "image_urls": len(results),
            "cards": 0,
            "cards_with_uploader": 0,
            "cards_with_date": 0,
            "cards_with_both": 0,
            "matched": 0,
            "year_inferred": 0,
        }
        self.last_diagnostics = diagnostics

        if not html or not results:
            return results

        if not BS4_AVAILABLE:
            diagnostics["error"] = (
                "未安装 beautifulsoup4（pip install beautifulsoup4）："
                + (BS4_ERROR or "import failed")
            )
            return results

        try:
            soup = BeautifulSoup(html, "html.parser")
            cards = self._find_card_nodes(soup)
        except Exception as exc:
            diagnostics["error"] = f"{type(exc).__name__}: {exc}"
            return results

        by_canonical = {}
        by_basename = {}

        for index, card in enumerate(cards):
            try:
                record = self._card_record(card, infer_year=infer_year)
            except Exception:
                continue
            if not record["image_urls"]:
                continue

            diagnostics["cards"] += 1
            if record["uploader"]:
                diagnostics["cards_with_uploader"] += 1
            if record["published_at"]:
                diagnostics["cards_with_date"] += 1
            if record["year_inferred"]:
                diagnostics["year_inferred"] += 1
            if record["uploader"] and record["published_at"]:
                diagnostics["cards_with_both"] += 1

            meta = {
                "uploader": record["uploader"],
                "user_id": record["user_id"],
                "photo_id": record["photo_id"],
                "photo_index": index,
                "published_at": record["published_at"],
                "published_raw": record["published_raw"],
                "year_inferred": record["year_inferred"],
                "metadata_source": record["source"],
                "confidence": record["confidence"],
            }
            for url in record["image_urls"]:
                key = canonical_image_url(url)
                if key:
                    by_canonical.setdefault(key, []).append(meta)
                base = image_basename(url)
                if base:
                    by_basename.setdefault(base, []).append(meta)

        for key in results:
            meta = self._best_meta(by_canonical.get(key))
            if meta is None:
                base = image_basename(key)
                if base:
                    meta = self._best_meta(by_basename.get(base))
            if meta is not None:
                results[key] = meta
                diagnostics["matched"] += 1

        return results



# ============================================================
# SQLite DB
# ============================================================

class ArchiveDB:

    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.create_tables()

    def create_tables(self):
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS shops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id TEXT UNIQUE,
                    shop_name TEXT,
                    city TEXT,
                    source_url TEXT,
                    resolved_url TEXT,
                    photo_count INTEGER DEFAULT 0,
                    oldest_photo_at TEXT DEFAULT '',
                    last_checked_at REAL,
                    created_at REAL,
                    updated_at REAL
                );

                CREATE TABLE IF NOT EXISTS candidates (
                    shop_id TEXT PRIMARY KEY,
                    shop_name TEXT DEFAULT '',
                    city TEXT DEFAULT '',
                    photo_count INTEGER DEFAULT 0,
                    oldest_photo_at TEXT DEFAULT '',
                    estimated_years REAL DEFAULT 0,
                    source TEXT DEFAULT '',
                    status TEXT DEFAULT 'candidate',
                    note TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    created_at REAL,
                    updated_at REAL
                );

                CREATE TABLE IF NOT EXISTS shop_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id TEXT,
                    original_url TEXT,
                    resolved_url TEXT,
                    link_type TEXT,
                    created_at REAL
                );

                CREATE TABLE IF NOT EXISTS pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id TEXT,
                    page_no INTEGER,
                    url TEXT,
                    local_html TEXT,
                    status TEXT,
                    image_count INTEGER DEFAULT 0,
                    downloaded_count INTEGER DEFAULT 0,
                    created_at REAL,
                    updated_at REAL,
                    UNIQUE(shop_id, page_no)
                );

                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id TEXT,
                    page_no INTEGER,
                    image_url TEXT,
                    canonical_url TEXT,
                    local_path TEXT,
                    sha256 TEXT,
                    phash TEXT,
                    width INTEGER DEFAULT 0,
                    height INTEGER DEFAULT 0,
                    filesize INTEGER DEFAULT 0,
                    uploader TEXT DEFAULT '',
                    published_at TEXT DEFAULT '',
                    metadata_source TEXT DEFAULT '',
                    photo_id TEXT DEFAULT '',
                    photo_index INTEGER DEFAULT -1,
                    published_raw TEXT DEFAULT '',
                    year_inferred INTEGER DEFAULT 0,
                    metadata_confidence INTEGER DEFAULT 0,
                    duplicate_of INTEGER,
                    status TEXT,
                    created_at REAL
                );

                CREATE TABLE IF NOT EXISTS image_content (
                    sha256 TEXT PRIMARY KEY,
                    image_id INTEGER,
                    phash TEXT,
                    width INTEGER DEFAULT 0,
                    height INTEGER DEFAULT 0,
                    filesize INTEGER DEFAULT 0,
                    local_path TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_pages_shop ON pages(shop_id);
                CREATE INDEX IF NOT EXISTS idx_images_shop_page ON images(shop_id, page_no);
                CREATE INDEX IF NOT EXISTS idx_images_sha ON images(sha256);
                CREATE INDEX IF NOT EXISTS idx_images_phash ON images(phash);
                CREATE INDEX IF NOT EXISTS idx_images_canonical ON images(canonical_url);
                CREATE INDEX IF NOT EXISTS idx_shops_city ON shops(city);
                CREATE INDEX IF NOT EXISTS idx_shops_name ON shops(shop_name);
                """
            )

            # 兼容旧版 archive.db：自动补充图片元数据字段
            self._ensure_column("images", "uploader", "TEXT DEFAULT ''")
            self._ensure_column("images", "published_at", "TEXT DEFAULT ''")
            self._ensure_column("images", "metadata_source", "TEXT DEFAULT ''")
            self._ensure_column("images", "photo_id", "TEXT DEFAULT ''")
            self._ensure_column("images", "photo_index", "INTEGER DEFAULT -1")
            self._ensure_column("images", "published_raw", "TEXT DEFAULT ''")
            self._ensure_column("images", "year_inferred", "INTEGER DEFAULT 0")
            self._ensure_column(
                "images", "metadata_confidence", "INTEGER DEFAULT 0"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_images_photo_id "
                "ON images(photo_id)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_status "
                "ON candidates(status)"
            )

            # 兼容旧版 archive.db：shops 表补充商户信息字段
            self._ensure_column("shops", "photo_count", "INTEGER DEFAULT 0")
            self._ensure_column("shops", "oldest_photo_at", "TEXT DEFAULT ''")
            self._ensure_column("shops", "last_checked_at", "REAL")

            self.conn.commit()

    def _ensure_column(self, table, column, definition):
        columns = {
            row[1]
            for row in self.conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        if column not in columns:
            self.conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def close(self):
        with self.lock:
            try:
                self.conn.commit()
                self.conn.close()
            except Exception:
                pass

    def upsert_shop(self, shop_id, shop_name=None, city=None,
                    source_url=None, resolved_url=None,
                    photo_count=None, oldest_photo_at=None):
        now = time.time()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO shops (
                    shop_id, shop_name, city, source_url, resolved_url,
                    photo_count, oldest_photo_at, last_checked_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(shop_id) DO UPDATE SET
                    shop_name=COALESCE(NULLIF(excluded.shop_name, ''), shop_name),
                    city=COALESCE(NULLIF(excluded.city, ''), city),
                    source_url=COALESCE(excluded.source_url, source_url),
                    resolved_url=COALESCE(excluded.resolved_url, resolved_url),
                    photo_count=COALESCE(excluded.photo_count, photo_count),
                    oldest_photo_at=COALESCE(
                        NULLIF(excluded.oldest_photo_at, ''), oldest_photo_at
                    ),
                    updated_at=excluded.updated_at
                """,
                (
                    shop_id, shop_name or "", city or "",
                    source_url, resolved_url,
                    int(photo_count) if photo_count else None,
                    oldest_photo_at or "", now, now, now,
                ),
            )
            self.conn.commit()

    def update_shop_meta(self, shop_id, shop_name="", city="",
                         photo_count=0, oldest_photo_at=""):
        """
        写入/补充商户名称、城市、照片总数、最早照片日期。返回是否真有改动。

        最早照片日期取「更早」的那个；名称/城市只在有值时覆盖，避免把已有信息擦掉。
        """
        if not shop_id:
            return False
        now = time.time()
        with self.lock:
            row = self.conn.execute(
                """
                SELECT COALESCE(shop_name, ''), COALESCE(city, ''),
                       COALESCE(photo_count, 0), COALESCE(oldest_photo_at, '')
                FROM shops WHERE shop_id=?
                """,
                (shop_id,),
            ).fetchone()
            if row is None:
                self.upsert_shop(
                    shop_id, shop_name=shop_name, city=city,
                    photo_count=photo_count, oldest_photo_at=oldest_photo_at,
                )
                return True

            new_name = shop_name or row[0]
            new_city = city or row[1]
            new_count = int(photo_count) if photo_count else row[2]
            known = [x for x in (row[3], oldest_photo_at) if x]
            new_oldest = min(known) if known else ""

            if (new_name, new_city, new_count, new_oldest) == row:
                self.conn.execute(
                    "UPDATE shops SET last_checked_at=? WHERE shop_id=?",
                    (now, shop_id),
                )
                self.conn.commit()
                return False

            self.conn.execute(
                """
                UPDATE shops
                SET shop_name=?, city=?, photo_count=?, oldest_photo_at=?,
                    last_checked_at=?, updated_at=?
                WHERE shop_id=?
                """,
                (new_name, new_city, new_count, new_oldest, now, now, shop_id),
            )
            self.conn.commit()
            return True

    # ---------- 候选店铺（自动找店结果） ----------

    def upsert_candidate(self, shop_id, shop_name="", city="",
                         photo_count=0, oldest_photo_at="",
                         estimated_years=0.0, source="",
                         status="candidate", note="", url=""):
        if not shop_id:
            return
        now = time.time()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO candidates (
                    shop_id, shop_name, city, photo_count, oldest_photo_at,
                    estimated_years, source, status, note, url,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(shop_id) DO UPDATE SET
                    shop_name=COALESCE(NULLIF(excluded.shop_name, ''), shop_name),
                    city=COALESCE(NULLIF(excluded.city, ''), city),
                    photo_count=COALESCE(excluded.photo_count, photo_count),
                    oldest_photo_at=COALESCE(
                        NULLIF(excluded.oldest_photo_at, ''), oldest_photo_at
                    ),
                    estimated_years=CASE
                        WHEN excluded.estimated_years > 0
                        THEN excluded.estimated_years
                        ELSE estimated_years
                    END,
                    source=COALESCE(NULLIF(excluded.source, ''), source),
                    status=CASE
                        WHEN candidates.status IN ('collected', 'ignored')
                        THEN candidates.status
                        ELSE excluded.status
                    END,
                    note=COALESCE(NULLIF(excluded.note, ''), note),
                    url=COALESCE(NULLIF(excluded.url, ''), url),
                    updated_at=excluded.updated_at
                """,
                (
                    shop_id, shop_name or "", city or "", int(photo_count or 0),
                    oldest_photo_at or "", float(estimated_years or 0),
                    source or "", status or "candidate", note or "", url or "",
                    now, now,
                ),
            )
            self.conn.commit()

    def get_candidates(self, status=None, limit=None):
        sql = """
            SELECT shop_id, COALESCE(shop_name, ''), COALESCE(city, ''),
                   COALESCE(photo_count, 0), COALESCE(oldest_photo_at, ''),
                   COALESCE(estimated_years, 0), COALESCE(source, ''),
                   COALESCE(status, ''), COALESCE(note, '')
            FROM candidates
        """
        params = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY estimated_years DESC, photo_count DESC, shop_id ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def set_candidate_status(self, shop_id, status, note=""):
        with self.lock:
            self.conn.execute(
                """
                UPDATE candidates
                SET status=?, note=CASE WHEN ? != '' THEN ? ELSE note END,
                    updated_at=?
                WHERE shop_id=?
                """,
                (status, note, note, time.time(), shop_id),
            )
            self.conn.commit()

    def clear_candidates(self, status=None):
        with self.lock:
            if status:
                cursor = self.conn.execute(
                    "DELETE FROM candidates WHERE status=?", (status,)
                )
            else:
                cursor = self.conn.execute("DELETE FROM candidates")
            self.conn.commit()
            return cursor.rowcount

    def candidates_stats(self):
        with self.lock:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM candidates"
            ).fetchone()[0]
            collected = self.conn.execute(
                "SELECT COUNT(*) FROM candidates WHERE status='collected'"
            ).fetchone()[0]
            return {"total": total, "collected": collected}

    def add_shop_link(self, shop_id, original_url, resolved_url, link_type):
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO shop_links (
                    shop_id, original_url, resolved_url,
                    link_type, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (shop_id, original_url, resolved_url, link_type, time.time()),
            )
            self.conn.commit()

    def get_shops(self):
        with self.lock:
            return self.conn.execute(
                """
                SELECT
                    shops.shop_id,
                    COALESCE(shops.shop_name, ''),
                    COALESCE(shops.city, ''),
                    COUNT(DISTINCT pages.id),
                    COUNT(DISTINCT images.id),
                    COALESCE(shops.photo_count, 0),
                    COALESCE(shops.oldest_photo_at, '')
                FROM shops
                LEFT JOIN pages ON shops.shop_id = pages.shop_id
                LEFT JOIN images ON shops.shop_id = images.shop_id
                GROUP BY shops.shop_id, shops.shop_name, shops.city
                ORDER BY
                    CASE WHEN shops.city IS NULL OR shops.city = '' THEN 1 ELSE 0 END,
                    shops.city,
                    shops.shop_name,
                    shops.shop_id
                """
            ).fetchall()

    def get_shop(self, shop_id):
        with self.lock:
            return self.conn.execute(
                """
                SELECT shop_id, shop_name, city, source_url, resolved_url,
                       COALESCE(photo_count, 0),
                       COALESCE(oldest_photo_at, '')
                FROM shops WHERE shop_id=?
                """,
                (shop_id,),
            ).fetchone()

    def upsert_page(self, shop_id, page_no, url, local_html=None,
                    status=None, image_count=None, downloaded_count=None):
        now = time.time()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO pages (
                    shop_id, page_no, url, local_html, status,
                    image_count, downloaded_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(shop_id, page_no) DO UPDATE SET
                    url=excluded.url,
                    local_html=COALESCE(excluded.local_html, pages.local_html),
                    status=COALESCE(excluded.status, pages.status),
                    image_count=COALESCE(excluded.image_count, pages.image_count),
                    downloaded_count=COALESCE(
                        excluded.downloaded_count, pages.downloaded_count
                    ),
                    updated_at=excluded.updated_at
                """,
                (shop_id, page_no, url, local_html, status,
                 image_count, downloaded_count, now, now),
            )
            self.conn.commit()

    def page_status(self, shop_id, page_no):
        with self.lock:
            row = self.conn.execute(
                "SELECT status FROM pages WHERE shop_id=? AND page_no=?",
                (shop_id, page_no),
            ).fetchone()
            return row[0] if row else None

    def get_pages(self, shop_id):
        with self.lock:
            return self.conn.execute(
                """
                SELECT page_no, url, local_html, status,
                       image_count, downloaded_count
                FROM pages WHERE shop_id=?
                ORDER BY page_no DESC
                """,
                (shop_id,),
            ).fetchall()

    def add_image(self, shop_id, page_no, image_url, canonical_url,
                  local_path, sha256, phash, width, height, filesize,
                  uploader="", published_at="", metadata_source="",
                  photo_id="", photo_index=-1, published_raw="",
                  year_inferred=0, metadata_confidence=0,
                  duplicate_of=None, status="downloaded"):
        with self.lock:
            cur = self.conn.execute(
                """
                INSERT INTO images (
                    shop_id, page_no, image_url, canonical_url,
                    local_path, sha256, phash, width, height,
                    filesize, uploader, published_at, metadata_source,
                    photo_id, photo_index, published_raw, year_inferred,
                    metadata_confidence,
                    duplicate_of, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?)
                """,
                (shop_id, page_no, image_url, canonical_url,
                 local_path, sha256, phash, width, height,
                 filesize, uploader, published_at, metadata_source,
                 photo_id, photo_index, published_raw,
                 1 if year_inferred else 0, int(metadata_confidence or 0),
                 duplicate_of, status, time.time()),
            )
            image_id = cur.lastrowid
            self.conn.execute(
                """
                INSERT OR IGNORE INTO image_content (
                    sha256, image_id, phash, width, height,
                    filesize, local_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sha256, image_id, phash, width, height,
                 filesize, local_path),
            )
            self.conn.commit()
            return image_id

    def find_sha(self, sha256):
        with self.lock:
            return self.conn.execute(
                """
                SELECT image_id, phash, width, height, filesize, local_path
                FROM image_content WHERE sha256=?
                """,
                (sha256,),
            ).fetchone()

    def find_phash_candidates(self, phash):
        if not phash:
            return []
        with self.lock:
            return self.conn.execute(
                """
                SELECT image_id, sha256, phash, width, height,
                       filesize, local_path
                FROM image_content WHERE phash IS NOT NULL
                """
            ).fetchall()

    def find_by_canonical_url(self, canonical_url):
        if not canonical_url:
            return None
        with self.lock:
            return self.conn.execute(
                """
                SELECT id, local_path, sha256, phash,
                       width, height, filesize,
                       uploader, published_at, metadata_source,
                       COALESCE(metadata_confidence, 0),
                       COALESCE(photo_id, '')
                FROM images
                WHERE canonical_url=?
                LIMIT 1
                """,
                (canonical_url,),
            ).fetchone()

    def update_image_metadata(
        self, image_id, uploader="", published_at="", metadata_source="",
        published_raw="", year_inferred=0, metadata_confidence=0,
        photo_id="", photo_index=None,
    ):
        """
        写入（或升级）单张图片的作者/时间，返回是否真的改动。

        规则：置信度更高的一次解析可以覆盖旧值；置信度相同或更低时只补空字段。
        V2.9 只会「填空」，所以一旦早期版本写错（把邻图的作者/时间写进来），
        重爬也无法修正；这里用 metadata_confidence 让更可靠的解析结果可以纠正它。
        """
        if not image_id:
            return False

        with self.lock:
            row = self.conn.execute(
                """
                SELECT COALESCE(uploader, ''), COALESCE(published_at, ''),
                       COALESCE(metadata_source, ''),
                       COALESCE(published_raw, ''),
                       COALESCE(year_inferred, 0),
                       COALESCE(metadata_confidence, 0),
                       COALESCE(photo_id, ''), COALESCE(photo_index, -1)
                FROM images WHERE id=?
                """,
                (image_id,),
            ).fetchone()
            if row is None:
                return False

            cur_uploader, cur_published = row[0], row[1]
            cur_source, cur_raw = row[2], row[3]
            cur_inferred = int(row[4] or 0)
            cur_confidence = int(row[5] or 0)
            cur_photo_id = row[6]
            cur_photo_index = int(row[7] or -1)

            new_confidence = int(metadata_confidence or 0)
            upgrade = new_confidence > cur_confidence

            def merge(new_value, current, clearable=False):
                if new_value:
                    return new_value if (upgrade or not current) else current
                # 空值默认不覆盖；但「置信度更高」时允许清空，用于修正旧版的错值：
                # 例如卡片本身没有 /member/ 链接（匿名上传），说明旧值确实是错的。
                if clearable and upgrade:
                    return ""
                return current

            new_uploader = merge(uploader, cur_uploader, clearable=True)
            new_published = merge(published_at, cur_published, clearable=True)
            new_source = merge(metadata_source, cur_source)
            new_raw = merge(published_raw, cur_raw)
            new_inferred = (
                1 if (year_inferred and new_published) else cur_inferred
            )
            new_photo_id = merge(photo_id, cur_photo_id)
            new_photo_index = cur_photo_index
            if photo_index is not None:
                try:
                    candidate = int(photo_index)
                except (TypeError, ValueError):
                    candidate = -1
                if candidate >= 0 or cur_photo_index < 0:
                    new_photo_index = candidate

            changed = (
                new_uploader != cur_uploader
                or new_published != cur_published
                or new_source != cur_source
                or new_raw != cur_raw
                or new_inferred != cur_inferred
                or new_photo_id != cur_photo_id
                or new_photo_index != cur_photo_index
            )
            if not changed:
                return False

            self.conn.execute(
                """
                UPDATE images
                SET uploader=?, published_at=?, metadata_source=?,
                    published_raw=?, year_inferred=?, metadata_confidence=?,
                    photo_id=?, photo_index=?
                WHERE id=?
                """,
                (
                    new_uploader, new_published, new_source, new_raw,
                    new_inferred, max(cur_confidence, new_confidence),
                    new_photo_id, new_photo_index, image_id,
                ),
            )
            self.conn.commit()
            return True

    def update_image_content(self, old_sha, new_sha, new_phash,
                             new_width, new_height,
                             new_filesize, new_local_path,
                             uploader="", published_at="",
                             metadata_source="", published_raw="",
                             year_inferred=0, metadata_confidence=0,
                             photo_id="", photo_index=None):
        if not old_sha:
            return
        with self.lock:
            row = self.conn.execute(
                """
                SELECT COALESCE(uploader, ''), COALESCE(published_at, ''),
                       COALESCE(metadata_source, ''),
                       COALESCE(published_raw, ''),
                       COALESCE(year_inferred, 0),
                       COALESCE(metadata_confidence, 0),
                       COALESCE(photo_id, ''), COALESCE(photo_index, -1)
                FROM images WHERE sha256=? LIMIT 1
                """,
                (old_sha,),
            ).fetchone()

            new_confidence = int(metadata_confidence or 0)
            if row is None:
                merged_uploader, merged_published = uploader, published_at
                merged_source, merged_raw = metadata_source, published_raw
                merged_inferred = 1 if year_inferred else 0
                merged_photo_id = photo_id
                merged_photo_index = -1 if photo_index is None else photo_index
                final_confidence = new_confidence
            else:
                cur_confidence = int(row[5] or 0)
                upgrade = new_confidence > cur_confidence

                def merge(new_value, current, clearable=False):
                    if new_value:
                        return new_value if (upgrade or not current) else current
                    if clearable and upgrade:
                        return ""
                    return current

                merged_uploader = merge(uploader, row[0], clearable=True)
                merged_published = merge(published_at, row[1], clearable=True)
                merged_source = merge(metadata_source, row[2])
                merged_raw = merge(published_raw, row[3])
                merged_inferred = (
                    1 if (year_inferred and merged_published)
                    else int(row[4] or 0)
                )
                merged_photo_id = merge(photo_id, row[6])
                merged_photo_index = int(row[7] or -1)
                if photo_index is not None:
                    try:
                        candidate = int(photo_index)
                    except (TypeError, ValueError):
                        candidate = -1
                    if candidate >= 0 or merged_photo_index < 0:
                        merged_photo_index = candidate
                final_confidence = max(cur_confidence, new_confidence)

            self.conn.execute(
                """
                UPDATE image_content
                SET sha256=?, phash=?, width=?, height=?,
                    filesize=?, local_path=?
                WHERE sha256=?
                """,
                (new_sha, new_phash, new_width, new_height,
                 new_filesize, new_local_path, old_sha),
            )
            self.conn.execute(
                """
                UPDATE images
                SET sha256=?, phash=?, width=?, height=?,
                    filesize=?, local_path=?,
                    uploader=?, published_at=?, metadata_source=?,
                    published_raw=?, year_inferred=?, metadata_confidence=?,
                    photo_id=?, photo_index=?
                WHERE sha256=?
                """,
                (new_sha, new_phash, new_width, new_height,
                 new_filesize, new_local_path,
                 merged_uploader, merged_published, merged_source,
                 merged_raw, merged_inferred, final_confidence,
                 merged_photo_id, merged_photo_index,
                 old_sha),
            )
            self.conn.commit()

    def get_images(self, shop_id, page_no=None):
        with self.lock:
            if page_no is None:
                return self.conn.execute(
                    """
                    SELECT id, page_no, image_url, local_path, sha256,
                           phash, width, height, filesize,
                           uploader, published_at, metadata_source,
                           duplicate_of, status,
                           COALESCE(photo_id, ''), COALESCE(photo_index, -1),
                           COALESCE(published_raw, ''),
                           COALESCE(year_inferred, 0),
                           COALESCE(metadata_confidence, 0)
                    FROM images WHERE shop_id=?
                    ORDER BY page_no DESC, id ASC
                    """,
                    (shop_id,),
                ).fetchall()
            return self.conn.execute(
                """
                SELECT id, page_no, image_url, local_path, sha256,
                       phash, width, height, filesize,
                       uploader, published_at, metadata_source,
                       duplicate_of, status,
                       COALESCE(photo_id, ''), COALESCE(photo_index, -1),
                       COALESCE(published_raw, ''),
                       COALESCE(year_inferred, 0),
                       COALESCE(metadata_confidence, 0)
                FROM images WHERE shop_id=? AND page_no=?
                ORDER BY id ASC
                """,
                (shop_id, page_no),
            ).fetchall()

    def get_all_image_paths(self):
        with self.lock:
            return [
                row[0] for row in self.conn.execute(
                    """
                    SELECT local_path FROM images
                    WHERE local_path IS NOT NULL AND local_path != ''
                    """
                ).fetchall()
            ]

    def stats(self):
        with self.lock:
            shops = self.conn.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
            pages = self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            images = self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            unique_images = self.conn.execute(
                "SELECT COUNT(*) FROM image_content"
            ).fetchone()[0]
            duplicates = self.conn.execute(
                "SELECT COUNT(*) FROM images WHERE duplicate_of IS NOT NULL"
            ).fetchone()[0]
            with_metadata = self.conn.execute(
                """
                SELECT COUNT(*) FROM images
                WHERE COALESCE(uploader, '') != ''
                   OR COALESCE(published_at, '') != ''
                """
            ).fetchone()[0]
            with_uploader = self.conn.execute(
                """
                SELECT COUNT(*) FROM images
                WHERE COALESCE(uploader, '') != ''
                """
            ).fetchone()[0]
            with_published = self.conn.execute(
                """
                SELECT COUNT(*) FROM images
                WHERE COALESCE(published_at, '') != ''
                """
            ).fetchone()[0]
            year_inferred = self.conn.execute(
                "SELECT COUNT(*) FROM images WHERE COALESCE(year_inferred, 0)=1"
            ).fetchone()[0]
            return {
                "shops": shops, "pages": pages, "images": images,
                "unique_images": unique_images, "duplicates": duplicates,
                "with_metadata": with_metadata,
                "with_uploader": with_uploader,
                "with_published": with_published,
                "year_inferred": year_inferred,
            }


# ============================================================
# 图片去重器（受设置开关控制）
# ============================================================

class ImageDeduplicator:

    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    def find_duplicate(self, sha256, phash, width, height, filesize):
        s = self.settings

        # ---------- SHA256 精确去重（可关闭） ----------
        if s.get("enable_sha256_dedup", True):
            exact = self.db.find_sha(sha256)
            if exact:
                return {
                    "type": "sha256", "image_id": exact[0],
                    "phash": exact[1], "width": exact[2],
                    "height": exact[3], "filesize": exact[4],
                    "local_path": exact[5],
                }

        # ---------- pHash 相似去重（可关闭） ----------
        if not s.get("enable_phash_dedup", True):
            return None

        if not phash:
            return None

        threshold = s.get("phash_threshold", 4)

        try:
            current_hash = imagehash.hex_to_hash(phash)
        except Exception:
            return None

        candidates = self.db.find_phash_candidates(phash)
        best = None
        best_distance = None

        for row in candidates:
            image_id, old_sha, old_phash, old_w, old_h, old_fs, old_path = row
            if not old_phash:
                continue
            try:
                old_hash = imagehash.hex_to_hash(old_phash)
            except Exception:
                continue
            distance = current_hash - old_hash
            if distance > threshold:
                continue
            if best is None or distance < best_distance:
                best_distance = distance
                best = {
                    "type": "phash", "image_id": image_id,
                    "sha256": old_sha, "phash": old_phash,
                    "width": old_w, "height": old_h,
                    "filesize": old_fs, "local_path": old_path,
                    "distance": distance,
                }
        return best


# ============================================================
# Crawl Engine
# ============================================================

class DianpingArchiveEngine:

    def __init__(self, db, http_client, resolver, settings,
                 logger=None, pause_event=None, stop_event=None):
        self.db = db
        self.http = http_client
        self.resolver = resolver
        self.settings = settings
        self.parser = DianpingHTMLParser(
            infer_year=settings.get("infer_year_for_short_date", True)
        )
        self.dedup = ImageDeduplicator(db, settings)
        self.logger = logger or (lambda x: None)
        self.pause_event = pause_event or threading.Event()
        self.stop_event = stop_event or threading.Event()

    def check_control(self):
        while self.pause_event.is_set():
            if self.stop_event.is_set():
                return False
            time.sleep(0.2)
        if self.stop_event.is_set():
            return False
        return True

    # ---------- 最后一页发现 ----------

    def discover_last_page(self, shop_id, session=None):
        s = self.settings
        page_cache = {}
        baseline_image_fps = None

        max_pages = s.get("max_page_discovery", 10000)
        verify_limit = s.get("verify_empty_limit", 2)
        min_valid_images = s.get("min_images_per_valid_page", 8)

        def extract_explicit_page_numbers(html):
            if not html:
                return []
            pages = set()

            for m in re.finditer(r'href="[^"]*photos\?pg=(\d+)', html, re.I):
                try:
                    pages.add(int(m.group(1)))
                except ValueError:
                    pass

            for m in re.finditer(r'data-page\s*=\s*["\']?(\d+)', html, re.I):
                try:
                    pages.add(int(m.group(1)))
                except ValueError:
                    pass

            for pattern in (
                r'"totalPage"\s*:\s*(\d+)',
                r'"total_page"\s*:\s*(\d+)',
                r'"pageCount"\s*:\s*(\d+)',
                r'"page_count"\s*:\s*(\d+)',
            ):
                for m in re.finditer(pattern, html):
                    try:
                        pages.add(int(m.group(1)))
                    except ValueError:
                        pass

            for m in re.finditer(r"共\s*(\d+)\s*页", html):
                try:
                    pages.add(int(m.group(1)))
                except ValueError:
                    pass

            return sorted(pages)

        def inspect_page(page):
            nonlocal baseline_image_fps

            if page in page_cache:
                return page_cache[page]

            if page < 1 or page > max_pages:
                result = {
                    "valid": False, "image_count": 0,
                    "image_urls": set(), "image_fps": set(),
                    "page_numbers": [], "reason": "out_of_range",
                }
                page_cache[page] = result
                return result

            url = page_url(shop_id, page)
            response = self.http.get_html(url)

            result = {
                "valid": False, "image_count": 0,
                "image_urls": set(), "image_fps": set(),
                "page_numbers": [], "reason": None,
            }

            if response is None or response.status_code != 200:
                result["reason"] = "http_error"
                page_cache[page] = result
                self.logger(f"[PROBE] page {page:<6} INVALID (HTTP)")
                return result

            html = response.text or ""
            lower_html = html.lower()

            if "login-redirect" in lower_html or (
                "/login" in lower_html and "password" in lower_html
            ):
                result["reason"] = "login_page"
                page_cache[page] = result
                self.logger(f"[PROBE] page {page:<6} INVALID (登录页)")
                return result

            invalid_keywords = [
                "页面不存在", "网页不存在",
                "您访问的页面不存在", "抱歉，页面不存在",
            ]
            if any(kw.lower() in lower_html for kw in invalid_keywords):
                result["reason"] = "not_found"
                page_cache[page] = result
                self.logger(f"[PROBE] page {page:<6} INVALID (页面不存在)")
                return result

            shop_patterns = [
                f"/shop/{shop_id}",
                f'shopid="{shop_id}"',
                f"shopId={shop_id}",
            ]
            if not any(p.lower() in lower_html for p in shop_patterns):
                result["reason"] = "shop_mismatch"
                page_cache[page] = result
                self.logger(f"[PROBE] page {page:<6} INVALID (shop 不匹配)")
                return result

            image_urls = self.parser.extract_image_urls(html)
            real_images = set()

            for img in image_urls:
                if not img:
                    continue
                u = img.lower()
                if any(x in u for x in IMAGE_BLACKLIST):
                    continue
                if "dianping.com" in u or "dpfile.com" in u:
                    real_images.add(img)

            if len(real_images) < min_valid_images:
                result["reason"] = (
                    f"too_few_images({len(real_images)}<"
                    f"{min_valid_images})"
                )
                page_cache[page] = result
                self.logger(
                    f"[PROBE] page {page:<6} "
                    f"INVALID 图片数 {len(real_images)} 低于阈值"
                )
                return result

            fps = {image_url_fingerprint(u) for u in real_images}

            if (
                baseline_image_fps is not None
                and page != 1
                and fps == baseline_image_fps
            ):
                result["reason"] = "same_as_page_1"
                page_cache[page] = result
                self.logger(
                    f"[PROBE] page {page:<6} INVALID (与第 1 页相同)"
                )
                return result

            result["valid"] = True
            result["image_count"] = len(real_images)
            result["image_urls"] = real_images
            result["image_fps"] = fps
            result["page_numbers"] = extract_explicit_page_numbers(html)
            result["html"] = html

            page_cache[page] = result
            self.logger(
                f"[PROBE] page {page:<6} VALID images={result['image_count']}"
            )
            return result

        # ---- 主流程 ----

        self.logger("=" * 50)
        self.logger("[DISCOVERY] 快速寻找真实最后一页")
        self.logger(f"[DISCOVERY] Shop ID: {shop_id}")

        start_page = 1
        first = inspect_page(start_page)

        if not first["valid"]:
            reason = first.get("reason") or "unknown"

            if reason == "login_page":
                raise RuntimeError(
                    "请求被重定向到登录页。\n"
                    "建议：\n"
                    "  1. 浏览器登录 dianping.com\n"
                    "  2. 手动打开相册页，复制真实 URL\n"
                    "  3. 或改用「图片数量」模式"
                )

            if reason.startswith("too_few_images"):
                raise RuntimeError(
                    "第 1 页提取到的疑似相册图片过少。\n"
                    f"原因：{reason}\n\n"
                    "请改用「图片数量」模式。"
                )

            raise RuntimeError(f"起始页无效。原因：{reason}")

        last_valid = start_page
        baseline_image_fps = set(first.get("image_fps", set()))
        self.logger(
            f"[DISCOVERY] 第 1 页建立基准，"
            f"图片指纹 {len(baseline_image_fps)} 个"
        )

        # ---------- 快速通道：用相册内嵌的照片总数推算末页 ----------
        # 相册页内嵌 'albumPicCount'，实测 末页 = ceil(总数 / 每页数) 且完全精确
        # （5/5 家商户：末页卡片数正好等于 总数 - (末页-1)*每页数）。
        # 相册整体是「新→旧」有序的，所以末页同时就是「最老照片」所在页，
        # 可以用来估算商户至少经营了多少年。
        shop_meta = parse_shop_meta_from_album(first.get("html") or "")
        if shop_meta["shop_name"] or shop_meta["city"] or shop_meta["photo_count"]:
            self.db.update_shop_meta(
                shop_id,
                shop_name=shop_meta["shop_name"],
                city=shop_meta["city"],
                photo_count=shop_meta["photo_count"],
            )
            self.logger(
                f"[DISCOVERY] 商户：{shop_meta['shop_name'] or '未命名商户'}"
                f"（{shop_meta['city'] or '未知城市'}）"
                f" 相册共 {shop_meta['photo_count'] or '?'} 张"
            )

        per_page = s.get("photos_per_page", 16) or 16
        guess_last = album_last_page(shop_meta.get("photo_count"), per_page)

        def oldest_date_of(html_text):
            dates = sorted(
                card["published_at"]
                for card in self.parser.extract_album_cards(html_text)
                if card["published_at"]
            )
            return dates[0] if dates else ""

        if guess_last == 1:
            oldest = oldest_date_of(first.get("html") or "")
            if oldest:
                self.db.update_shop_meta(shop_id, oldest_photo_at=oldest)
            self.logger(
                f"[DISCOVERY] 相册只有一页"
                + (f"，最早照片 {oldest}" if oldest else "")
            )
            self.logger("[DISCOVERY] Last valid album page: 1")
            self.logger("=" * 50)
            return 1

        if guess_last and 1 < guess_last <= max_pages:
            response_guess = self.http.get_html(page_url(shop_id, guess_last))
            guessed_html = ""
            if response_guess is not None and response_guess.status_code == 200:
                guessed_html = response_guess.text or ""
            guessed_cards = (
                self.parser.extract_album_cards(guessed_html)
                if guessed_html else []
            )
            expected_cards = int(shop_meta["photo_count"]) - (guess_last - 1) * per_page
            if guessed_cards:
                oldest = oldest_date_of(guessed_html)
                if oldest:
                    self.db.update_shop_meta(shop_id, oldest_photo_at=oldest)
                self.logger(
                    f"[DISCOVERY] 由照片总数 {shop_meta['photo_count']} 推算末页 "
                    f"{guess_last}：该页 {len(guessed_cards)} 张"
                    f"（应为 {expected_cards} 张）"
                    + (f"，最早照片 {oldest}" if oldest else "")
                )
                self.logger(f"[DISCOVERY] Last valid album page: {guess_last}")
                self.logger("=" * 50)
                return guess_last
            self.logger(
                f"[DISCOVERY] 推算末页 {guess_last} 没有图片，改用二分查找"
            )

        second = inspect_page(2)
        if second.get("reason") == "same_as_page_1":
            raise RuntimeError(
                "无法通过 HTTP 请求检测真实分页。\n"
                "原因：第 1 页和第 2 页返回了完全相同的图片集合。\n"
                "请改用「图片数量」模式。"
            )

        explicit_pages = first.get("page_numbers", [])
        if explicit_pages:
            explicit_max = max(explicit_pages)
            if explicit_max > start_page:
                self.logger(
                    f"[DISCOVERY] HTML 明示最大页码: {explicit_max}"
                )
                result = inspect_page(explicit_max)
                if result["valid"]:
                    last_valid = explicit_max

        low = last_valid
        step = 1
        high = None

        self.logger("[DISCOVERY] Exponential search...")
        while True:
            probe = low + step
            if probe > max_pages:
                probe = max_pages
            result = inspect_page(probe)
            if result["valid"]:
                low = probe
                if probe >= max_pages:
                    high = None
                    break
                step *= 2
            else:
                high = probe
                break

        if high is not None:
            self.logger(
                f"[DISCOVERY] Binary search range: {low} ~ {high}"
            )
            while high - low > 1:
                mid = low + (high - low) // 2
                result = inspect_page(mid)
                if result["valid"]:
                    low = mid
                else:
                    high = mid
            last_valid = low
        else:
            last_valid = low

        self.logger("[DISCOVERY] Final verification...")
        verification_results = []
        for offset in range(1, verify_limit + 1):
            page = last_valid + offset
            if page > max_pages:
                break
            result = inspect_page(page)
            verification_results.append((page, result["valid"]))
            if result["valid"]:
                last_valid = page
                break

        if any(v for _, v in verification_results):
            low = last_valid
            step = 1
            high = None
            while True:
                probe = low + step
                if probe > max_pages:
                    probe = max_pages
                result = inspect_page(probe)
                if result["valid"]:
                    low = probe
                    if probe >= max_pages:
                        break
                    step *= 2
                else:
                    high = probe
                    break
            if high is not None:
                while high - low > 1:
                    mid = low + (high - low) // 2
                    result = inspect_page(mid)
                    if result["valid"]:
                        low = mid
                    else:
                        high = mid
                last_valid = low

        self.logger(f"[DISCOVERY] Last valid album page: {last_valid}")
        self.logger(f"[DISCOVERY] Pages requested: {len(page_cache)}")
        self.logger("=" * 50)
        return last_valid

    # ---------- 页面采集 ----------

    def crawl_page(self, shop_id, page_no, output_dir):
        if not self.check_control():
            return False

        s = self.settings

        page_delay = s.get("page_delay", 1.0)
        image_delay = s.get("image_delay", 0.35)
        max_per_page = s.get("max_images_per_page", 500)

        enable_in_page = s.get("enable_in_page_dedup", True)
        enable_url_dedup = s.get("enable_url_dedup", True)
        enable_size_filter = s.get("enable_size_filter", True)
        enable_replace = s.get("enable_replace_higher", True)
        min_dim = s.get("min_image_dimension", 200)

        url = page_url(shop_id, page_no)
        self.logger(f"[PAGE] {shop_id} / {page_no}")

        response = self.http.get_html(url)
        if not response:
            self.db.upsert_page(shop_id, page_no, url, status="request_failed")
            self.logger("[PAGE] HTTP 请求失败")
            return False

        html = response.text or ""

        page_dir = Path(output_dir) / f"page_{page_no}"
        page_dir.mkdir(parents=True, exist_ok=True)

        html_path = page_dir / "page.html"
        try:
            html_path.write_text(html, encoding="utf-8")
        except Exception:
            pass

        raw_image_urls = self.parser.extract_image_urls(html)[:max_per_page]

        use_original = s.get("use_original_image_url", True)
        asset_map = (
            self.parser.extract_photo_assets(html) if use_original else {}
        )
        if asset_map:
            self.logger(
                f"[META] 页面内嵌原图数据 {len(asset_map)} 条，"
                f"下载时将优先取原图"
            )

        # ---------- 图片发布者 / 发布时间 ----------
        infer_year = s.get("infer_year_for_short_date", True)
        metadata_map = self.parser.extract_image_metadata(
            html, raw_image_urls, infer_year=infer_year
        )
        diagnostics = dict(getattr(self.parser, "last_diagnostics", {}) or {})

        if not BS4_AVAILABLE:
            self.logger(
                "[META][警告] 未安装 beautifulsoup4，无法结构化解析相册卡片，"
                "本页不会写入作者/时间。请执行：pip install beautifulsoup4"
            )
        elif diagnostics.get("error"):
            self.logger(f"[META][警告] 元数据解析异常：{diagnostics['error']}")

        metadata_count = sum(
            1
            for item in metadata_map.values()
            if item.get("uploader") or item.get("published_at")
        )
        uploader_count = sum(
            1 for item in metadata_map.values() if item.get("uploader")
        )
        date_count = sum(
            1 for item in metadata_map.values() if item.get("published_at")
        )
        self.logger(
            f"[META] 相册卡片 {diagnostics.get('cards', 0)} 个"
            f"（含作者 {diagnostics.get('cards_with_uploader', 0)} / "
            f"含时间 {diagnostics.get('cards_with_date', 0)}）；"
            f"绑定到图片：作者 {uploader_count}、时间 {date_count}，"
            f"共 {len(raw_image_urls)} 张，其中年份推算 "
            f"{diagnostics.get('year_inferred', 0)} 张"
        )
        if raw_image_urls and metadata_count == 0:
            self.logger(
                "[META][警告] 本页未解析到任何作者/时间。请确认已安装 "
                "beautifulsoup4，并用"
                "「python 本脚本 --parse-html <page.html>」做自检。"
            )

        # ---------- 自动获取商户名称 / 城市 / 照片总数 ----------
        shop_meta = parse_shop_meta_from_album(html)
        if shop_meta["shop_name"] or shop_meta["city"] or shop_meta["photo_count"]:
            updated = self.db.update_shop_meta(
                shop_id,
                shop_name=shop_meta["shop_name"],
                city=shop_meta["city"],
                photo_count=shop_meta["photo_count"],
            )
            page_dates = [
                item.get("published_at", "")
                for item in metadata_map.values()
                if item.get("published_at")
            ]
            if page_dates:
                self.db.update_shop_meta(
                    shop_id, oldest_photo_at=min(page_dates)
                )
            self.logger(
                f"[SHOP] {shop_meta['shop_name'] or '未命名商户'}"
                f"（{shop_meta['city'] or '未知城市'}）"
                f" 相册共 {shop_meta['photo_count'] or '?'} 张"
                + ("　已写入档案" if updated else "")
            )

        # ---------- 采集日期范围 ----------
        date_from = normalize_date_bound(s.get("date_from", ""))
        date_to = normalize_date_bound(s.get("date_to", ""))
        keep_unknown = bool(s.get("date_filter_keep_unknown", True))
        date_filter_on = bool(date_from or date_to)

        def date_allowed(metadata):
            if not date_filter_on:
                return True
            state = date_in_range(
                (metadata or {}).get("published_at", ""), date_from, date_to
            )
            if state is None:
                return keep_unknown
            return state

        if date_filter_on:
            self.logger(
                f"[DATE] 只收集 {date_from or '不限'} ~ {date_to or '不限'}"
                f"（日期未知的图片{'保留' if keep_unknown else '跳过'}）"
            )

        # ---------- 第 1 层：页内 URL 去重 ----------
        seen_canonical = set()
        image_urls = []
        if enable_in_page:
            for img_url in raw_image_urls:
                c = canonical_image_url(img_url)
                if c and c not in seen_canonical:
                    seen_canonical.add(c)
                    image_urls.append(img_url)
        else:
            image_urls = list(raw_image_urls)

        removed_in_page = len(raw_image_urls) - len(image_urls)

        # 整页都不在范围内的直接跳过（相册是「新→旧」有序的，可以省掉大量下载）
        if date_filter_on and metadata_map:
            page_dates = sorted(
                item.get("published_at", "")
                for item in metadata_map.values()
                if item.get("published_at")
            )
            if page_dates and not any(
                date_allowed(item) for item in metadata_map.values()
            ):
                self.logger(
                    f"[SKIP] 第 {page_no} 页全部不在日期范围内"
                    f"（本页 {page_dates[0]} ~ {page_dates[-1]}）"
                )
                self.db.upsert_page(
                    shop_id, page_no, url,
                    local_html=str(html_path),
                    status="skipped_by_date",
                    image_count=len(image_urls),
                    downloaded_count=0,
                )
                return True

        self.db.upsert_page(
            shop_id, page_no, url,
            local_html=str(html_path),
            status="downloading",
            image_count=len(image_urls),
        )

        self.logger(
            f"[PAGE] 提取 {len(raw_image_urls)} 个 URL，"
            f"页内去重后 {len(image_urls)} 个"
        )

        downloaded = 0
        skipped_url = 0
        skipped_size = 0
        skipped_date = 0
        replaced = 0

        manifest_columns = [
            "page", "index", "image_url", "canonical_url",
            "local_path", "sha256", "phash", "width", "height",
            "filesize", "uploader", "published_at", "metadata_source",
            "duplicate_of", "status", "photo_id", "photo_index",
            "published_raw", "year_inferred", "metadata_confidence",
            "download_url",
        ]

        manifest_path = page_dir / "manifest.csv"
        write_header = True
        if manifest_path.exists():
            header = []
            try:
                with open(manifest_path, "r", encoding="utf-8-sig",
                          newline="") as probe:
                    header = next(csv.reader(probe), [])
            except Exception:
                header = []
            if "metadata_confidence" in header:
                write_header = False
            else:
                # 旧版 manifest 的列不同，另存新文件，避免列错位
                manifest_path = page_dir / "manifest_v3.csv"
                write_header = not manifest_path.exists()

        manifest_file = open(manifest_path, "a", newline="", encoding="utf-8-sig")
        writer = csv.writer(manifest_file)

        if write_header:
            writer.writerow(manifest_columns)

        for index, image_url in enumerate(image_urls, start=1):
            if not self.check_control():
                manifest_file.close()
                self.db.upsert_page(
                    shop_id, page_no, url,
                    local_html=str(html_path), status="paused",
                    image_count=len(image_urls),
                    downloaded_count=downloaded,
                )
                return False

            canonical_url = canonical_image_url(image_url)

            metadata = metadata_map.get(canonical_url, {})
            meta_fields = metadata_fields(metadata)
            uploader = meta_fields["uploader"]
            published_at = meta_fields["published_at"]
            metadata_source = meta_fields["metadata_source"]

            def meta_columns(download_url=""):
                return [
                    meta_fields["photo_id"],
                    meta_fields["photo_index"],
                    meta_fields["published_raw"],
                    meta_fields["year_inferred"],
                    meta_fields["metadata_confidence"],
                    download_url,
                ]

            download_url = ""

            # ---------- 采集日期范围：范围外的图片直接跳过 ----------
            if date_filter_on and not date_allowed(metadata):
                writer.writerow([
                    page_no, index, image_url, canonical_url,
                    "", "", "", 0, 0, 0,
                    uploader, published_at, metadata_source,
                    "", f"skipped_by_date({published_at or '未知'})",
                ] + meta_columns(download_url))
                manifest_file.flush()
                skipped_date += 1
                continue

            # ---------- 第 2 层：URL 预去重 ----------
            if enable_url_dedup:
                existing = self.db.find_by_canonical_url(canonical_url)
                if existing:
                    existing_id = existing[0]
                    existing_path = existing[1]

                    # 旧记录缺元数据时补写；本次解析置信度更高时纠正旧值。
                    self.db.update_image_metadata(
                        existing_id, **meta_fields
                    )

                    writer.writerow([
                        page_no, index, image_url, canonical_url,
                        existing_path or "", "", "", 0, 0, 0,
                        uploader, published_at, metadata_source,
                        existing_id, "skipped_url_exists",
                    ] + meta_columns(download_url))
                    manifest_file.flush()
                    skipped_url += 1
                    continue

            self.logger(f"[IMAGE] {index}/{len(image_urls)}")

            # ---------- 下载：按画质从高到低尝试，全部失败才算失败 ----------
            candidates = [image_url]
            if use_original:
                candidates = image_download_candidates(
                    image_url, meta_fields["photo_id"], asset_map
                )

            data = None
            for candidate in candidates:
                data = self.http.get_bytes(candidate, referer=url)
                if data:
                    download_url = candidate
                    break

            if data and download_url != image_url:
                self.logger("[IMAGE] 已取到更高画质版本")
            if not data:
                download_url = ""

            if not data:
                writer.writerow([
                    page_no, index, image_url, canonical_url,
                    "", "", "", 0, 0, 0,
                    uploader, published_at, metadata_source,
                    "", "download_failed",
                ] + meta_columns(download_url))
                manifest_file.flush()
                time.sleep(image_delay)
                continue

            sha = sha256_bytes(data)
            width, height = image_dimensions(data)
            phash = calculate_phash(data)
            filesize = len(data)

            # ---------- 第 3 层：尺寸过滤 ----------
            if enable_size_filter and (
                width < min_dim or height < min_dim
            ):
                writer.writerow([
                    page_no, index, image_url, canonical_url,
                    "", sha, phash or "", width, height, filesize,
                    uploader, published_at, metadata_source,
                    "", f"skipped_too_small({width}x{height})",
                ] + meta_columns(download_url))
                manifest_file.flush()
                skipped_size += 1
                self.logger(
                    f"[IMAGE] 跳过（尺寸 {width}x{height} < {min_dim}）"
                )
                time.sleep(image_delay)
                continue

            # ---------- 第 4 层：SHA256 + pHash ----------
            duplicate = self.dedup.find_duplicate(
                sha, phash, width, height, filesize
            )

            if duplicate and duplicate["type"] == "sha256":
                duplicate_id = duplicate["image_id"]
                self.db.add_image(
                    shop_id=shop_id, page_no=page_no,
                    image_url=image_url, canonical_url=canonical_url,
                    local_path=duplicate["local_path"],
                    sha256=sha, phash=phash,
                    width=width, height=height, filesize=filesize,
                    duplicate_of=duplicate_id, status="duplicate_sha256",
                    **meta_fields
                )
                writer.writerow([
                    page_no, index, image_url, canonical_url,
                    duplicate["local_path"] or "",
                    sha, phash or "", width, height, filesize,
                    uploader, published_at, metadata_source,
                    duplicate_id, "duplicate_sha256",
                ] + meta_columns(download_url))
                manifest_file.flush()
                time.sleep(image_delay)
                continue

            if duplicate and duplicate["type"] == "phash":
                old_area = duplicate["width"] * duplicate["height"]
                new_area = width * height
                duplicate_id = duplicate["image_id"]

                if new_area > old_area and enable_replace:
                    # 新图更清晰 → 替换
                    filename = f"{index:05d}_{sha[:12]}.jpg"
                    local_path = page_dir / filename
                    try:
                        local_path.write_bytes(data)
                    except Exception:
                        manifest_file.close()
                        return False

                    old_path = duplicate["local_path"]
                    old_sha = duplicate.get("sha256")

                    self.db.update_image_content(
                        old_sha=old_sha,
                        new_sha=sha, new_phash=phash,
                        new_width=width, new_height=height,
                        new_filesize=filesize,
                        new_local_path=str(local_path),
                        **meta_fields
                    )

                    if old_path and Path(old_path).exists():
                        try:
                            Path(old_path).unlink()
                        except Exception:
                            pass

                    writer.writerow([
                        page_no, index, image_url, canonical_url,
                        str(local_path), sha, phash or "",
                        width, height, filesize,
                        uploader, published_at, metadata_source,
                        "", "replaced_higher_quality",
                    ] + meta_columns(download_url))
                    manifest_file.flush()
                    replaced += 1
                    self.logger(
                        f"[IMAGE] 替换更高分辨率 {old_area}→{new_area}"
                    )

                else:
                    self.db.add_image(
                        shop_id=shop_id, page_no=page_no,
                        image_url=image_url, canonical_url=canonical_url,
                        local_path=duplicate["local_path"],
                        sha256=sha, phash=phash,
                        width=width, height=height, filesize=filesize,
                        duplicate_of=duplicate_id, status="duplicate_phash",
                        **meta_fields
                    )
                    writer.writerow([
                        page_no, index, image_url, canonical_url,
                        duplicate["local_path"] or "",
                        sha, phash or "", width, height, filesize,
                        uploader, published_at, metadata_source,
                        duplicate_id, "duplicate_phash",
                    ] + meta_columns(download_url))
                manifest_file.flush()
                time.sleep(image_delay)
                continue

            # ---------- 新图片 ----------
            filename = f"{index:05d}_{sha[:12]}.jpg"
            local_path = page_dir / filename
            try:
                local_path.write_bytes(data)
            except Exception as e:
                self.logger(f"[IMAGE] 保存失败：{e}")
                continue

            self.db.add_image(
                shop_id=shop_id, page_no=page_no,
                image_url=image_url, canonical_url=canonical_url,
                local_path=str(local_path),
                sha256=sha, phash=phash,
                width=width, height=height, filesize=filesize,
                duplicate_of=None, status="downloaded",
                **meta_fields
            )

            writer.writerow([
                page_no, index, image_url, canonical_url,
                str(local_path), sha, phash or "",
                width, height, filesize,
                uploader, published_at, metadata_source,
                "", "downloaded",
            ] + meta_columns(download_url))
            manifest_file.flush()
            downloaded += 1
            time.sleep(image_delay)

        manifest_file.close()

        self.db.upsert_page(
            shop_id, page_no, url,
            local_html=str(html_path), status="completed",
            image_count=len(image_urls),
            downloaded_count=downloaded,
        )

        if (
            removed_in_page or skipped_url or skipped_size
            or skipped_date or replaced
        ):
            self.logger(
                f"[PAGE] 第 {page_no} 页汇总："
                f"页内去重 {removed_in_page}，"
                f"URL 重复跳过 {skipped_url}，"
                f"日期范围外跳过 {skipped_date}，"
                f"尺寸过小跳过 {skipped_size}，"
                f"替换高清 {replaced}，"
                f"新下载 {downloaded}"
            )

        self.logger(f"[PAGE] 完成：{page_no} 下载 {downloaded} 张")
        return True

    def crawl_shop(self, shop_id, output_root, start_page, stop_page):
        shop_dir = Path(output_root) / safe_filename(shop_id)
        shop_dir.mkdir(parents=True, exist_ok=True)

        page_delay = self.settings.get("page_delay", 1.0)

        if start_page >= stop_page:
            pages = range(start_page, stop_page - 1, -1)
        else:
            pages = range(start_page, stop_page + 1)

        total = abs(start_page - stop_page) + 1

        for i, page_no in enumerate(pages, start=1):
            if not self.check_control():
                return False
            self.logger(f"[PROGRESS] {i}/{total}")

            if (
                not self.settings.get("recrawl_completed_pages", False)
                and self.db.page_status(shop_id, page_no) == "completed"
            ):
                self.logger(f"[SKIP] 第 {page_no} 页已完成")
                continue

            if (
                self.settings.get("recrawl_completed_pages", False)
                and self.db.page_status(shop_id, page_no) == "completed"
            ):
                self.logger(
                    f"[REPAIR] 第 {page_no} 页重新解析，"
                    f"用于修正历史作者/时间"
                )

            self.crawl_page(shop_id, page_no, shop_dir)
            time.sleep(page_delay)

        return True


# ============================================================
# 采集控制器
# ============================================================

class CrawlController:

    def __init__(self, db, http_client, resolver, settings, logger):
        self.db = db
        self.http = http_client
        self.resolver = resolver
        self.settings = settings
        self.logger = logger
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
        self.running = False
        self.current_shop = None

    def start(self, input_url, output_dir, mode,
              start_page=None, stop_page=None, photo_count=None,
              extra_shop_ids=None):
        if self.running:
            self.logger("[CTRL] 当前已经在运行")
            return
        self.pause_event.clear()
        self.stop_event.clear()
        self.running = True
        self.thread = threading.Thread(
            target=self._worker,
            args=(input_url, output_dir, mode,
                  start_page, stop_page, photo_count, extra_shop_ids),
            daemon=True,
        )
        self.thread.start()

    def check_control(self):
        while self.pause_event.is_set():
            if self.stop_event.is_set():
                return False
            time.sleep(0.2)
        return not self.stop_event.is_set()

    def _build_targets(self, input_url, extra_shop_ids):
        """把「输入链接」和「候选店铺列表」合并成待采集的商户列表（去重）。"""
        targets = []
        input_text = (input_url or "").strip()
        if input_text:
            extracted = extract_dianping_url(input_text)
            if not extracted:
                self.logger(
                    "[ERROR] 输入中未找到有效的大众点评链接，"
                    "只处理候选店铺列表。"
                )
            else:
                self.logger(f"[URL] 提取到：{extracted}")
                targets.append(self.resolver.resolve(extracted))

        for shop_id in extra_shop_ids or []:
            shop_id = str(shop_id).strip()
            if not shop_id:
                continue
            targets.append({
                "input_url": page_url(shop_id, 1),
                "final_url": page_url(shop_id, 1),
                "shop_id": shop_id,
                "type": "photos",
                "from_candidate": True,
            })

        unique = []
        seen = set()
        for target in targets:
            shop_id = target.get("shop_id")
            if not shop_id or shop_id in seen:
                continue
            seen.add(shop_id)
            unique.append(target)
        return unique

    def _collect_one(self, result, output_dir, mode,
                     start_page, stop_page, photo_count):
        """采集单个商户（原来的单店流程）。"""
        self.logger(f"[URL] 类型：{result['type']}")
        self.logger(f"[URL] 最终地址：{result['final_url']}")

        shop_id = result.get("shop_id")
        if not shop_id:
            self.logger("[ERROR] 无法解析出 shop_id。")
            return False

        self.current_shop = shop_id

        self.db.upsert_shop(
            shop_id=shop_id,
            source_url=result["input_url"],
            resolved_url=result["final_url"],
        )
        self.db.add_shop_link(
            shop_id=shop_id,
            original_url=result["input_url"],
            resolved_url=result["final_url"],
            link_type=result["type"],
        )

        engine = DianpingArchiveEngine(
            db=self.db, http_client=self.http,
            resolver=self.resolver, settings=self.settings,
            logger=self.logger,
            pause_event=self.pause_event, stop_event=self.stop_event,
        )

        per_page = self.settings.get("photos_per_page", 16)

        if mode == "auto_all":
            last_page = engine.discover_last_page(shop_id)
            start_page = last_page
            stop_page = 1
        elif mode == "auto_to":
            last_page = engine.discover_last_page(shop_id)
            start_page = last_page
            if stop_page is None:
                raise ValueError("auto_to 模式缺少 stop_page")
        elif mode == "count_all":
            if photo_count is None:
                raise ValueError("count_all 模式缺少 photo_count")
            last_page = photos_count_to_last_page(photo_count, per_page)
            self.logger(
                f"[COUNT] {photo_count} 张 ÷ 每页 {per_page} "
                f"→ 最后一页 {last_page}"
            )
            start_page = last_page
            stop_page = 1
        elif mode == "count_to":
            if photo_count is None:
                raise ValueError("count_to 模式缺少 photo_count")
            if stop_page is None:
                raise ValueError("count_to 模式缺少 stop_page")
            last_page = photos_count_to_last_page(photo_count, per_page)
            self.logger(
                f"[COUNT] {photo_count} 张 ÷ 每页 {per_page} "
                f"→ 最后一页 {last_page}"
            )
            start_page = last_page
        elif mode == "custom":
            if start_page is None:
                raise ValueError("custom 模式缺少 start_page")
            if stop_page is None:
                raise ValueError("custom 模式缺少 stop_page")
        else:
            raise ValueError(f"未知模式：{mode}")

        engine.crawl_shop(
            shop_id=shop_id, output_root=output_dir,
            start_page=int(start_page),
            stop_page=int(stop_page),
        )

        if result.get("from_candidate"):
            self.db.set_candidate_status(shop_id, "collected")
        return True

    def _worker(self, input_url, output_dir, mode,
                start_page, stop_page, photo_count, extra_shop_ids=None):
        try:
            targets = self._build_targets(input_url, extra_shop_ids)
            if not targets:
                self.logger("[ERROR] 没有可采集的商户。")
                return

            total = len(targets)
            if total > 1:
                self.logger(f"[CTRL] 本次共 {total} 家商户")

            for position, result in enumerate(targets, start=1):
                if not self.check_control():
                    self.logger("[CTRL] 已停止")
                    break
                if total > 1:
                    self.logger(
                        f"[CTRL] ===== 第 {position}/{total} 家商户 ====="
                    )
                try:
                    self._collect_one(
                        result, output_dir, mode,
                        start_page, stop_page, photo_count,
                    )
                except Exception as exc:
                    self.logger(
                        f"[ERROR] {result.get('shop_id')} "
                        f"{type(exc).__name__}: {exc}"
                    )

            self.logger("[CTRL] 任务完成")

        except Exception as e:
            self.logger(f"[ERROR] {type(e).__name__}: {e}")
        finally:
            self.running = False
            self.current_shop = None

    def pause(self):
        if not self.running:
            return
        self.pause_event.set()
        self.logger("[CTRL] 已暂停")

    def resume(self):
        if not self.running:
            return
        self.pause_event.clear()
        self.logger("[CTRL] 已继续")

    def stop(self):
        if not self.running:
            return
        self.stop_event.set()
        self.pause_event.clear()
        self.logger("[CTRL] 正在停止...")


# ============================================================
# 自动找店
# ============================================================

class ShopDiscovery:
    """
    从「含有店铺链接的本地 HTML / 文本」里批量发现店铺，并逐店核验。

    每核验一家店只需要 2 次请求：相册首页（名称 / 城市 / 照片总数）+ 末页（最早的照片）。

    为什么不能直接抓点评的列表页来发现店铺（2026-09 实测，全部失败）：
      * www.dianping.com/{城市}/ch{N}、/search/keyword/…、/shop/<id>（商户主页）、
        /member/<id>（会员页）、/shop/<id>/officialphotos
        → 全部被美团风控拦截（跳到 verify.meituan.com「验证中心」）或返回 403；
      * 手机站 m.dianping.com 返回不含数据的 JS 骨架；
      * Bing/百度/搜狗/360 对普通 HTTP 客户端只返回 JS 页面（Bing 的 RSS 也无结果）；
      * 按 ID 邻近暴力扫描：实测 10 个邻居只有 0~1 家是有效店铺，且城市随机，
        效率极低又容易触发风控，因此没有把它做成功能。

    所以最实际的自动化路径是：用浏览器打开点评的列表页 / 搜索结果页，
    Ctrl+S「另存为」HTML（或者直接把链接复制出来），剩下的交给这里自动完成。
    """

    def __init__(self, http_client, settings, db=None, logger=None,
                 pause_event=None, stop_event=None, progress=None):
        self.http = http_client
        self.settings = settings
        self.db = db
        self.logger = logger or (lambda message: None)
        self.pause_event = pause_event or threading.Event()
        self.stop_event = stop_event or threading.Event()
        self.progress = progress

    # ---------- 基础 ----------

    def check_control(self):
        while self.pause_event.is_set():
            if self.stop_event.is_set():
                return False
            time.sleep(0.2)
        return not self.stop_event.is_set()

    def oldest_photo_at(self, html):
        dates = sorted(
            card["published_at"]
            for card in DianpingHTMLParser().extract_album_cards(html or "")
            if card["published_at"]
        )
        return dates[0] if dates else ""

    def probe_shop(self, shop_id, delay=None):
        """
        核验一家店铺，返回
        {shop_id, shop_name, city, photo_count, oldest_photo_at,
         estimated_years, url, error}
        """
        info = {
            "shop_id": str(shop_id), "shop_name": "", "city": "",
            "photo_count": 0, "oldest_photo_at": "", "estimated_years": None,
            "url": page_url(shop_id, 1), "error": "",
        }
        response = self.http.get_html(page_url(shop_id, 1))
        if response is None or response.status_code != 200:
            info["error"] = (
                "请求失败" if response is None
                else f"HTTP {response.status_code}"
            )
            return info

        html = response.text or ""
        if not html:
            info["error"] = "页面为空"
            return info
        if "该商户不存在或已被删除" in html:
            info["error"] = "店铺不存在或已删除"
            return info
        if "spiderindefence" in html or "验证中心" in html:
            info["error"] = "被点评风控拦截"
            return info

        meta = parse_shop_meta_from_album(html)
        if not meta["shop_name"]:
            # 相册页的标题里一定有商户名；没有就说明不是正常相册页
            # （无效 ID 会返回「出错」页，被删除的店返回「不存在」页）
            cards = DianpingHTMLParser().extract_album_cards(html)
            if not cards:
                info["error"] = "未找到商户信息（无效店铺 ID 或页面异常）"
                return info

        info["shop_name"] = meta["shop_name"]
        info["city"] = meta["city"]
        info["photo_count"] = meta["photo_count"]
        oldest = self.oldest_photo_at(html)

        per_page = self.settings.get("photos_per_page", 16) or 16
        last_page = album_last_page(meta["photo_count"], per_page)
        if last_page and last_page > 1:
            self._wait(delay)
            if not self.check_control():
                info["error"] = "已停止"
                return info
            response_last = self.http.get_html(page_url(shop_id, last_page))
            if response_last is not None and response_last.status_code == 200:
                oldest_last = self.oldest_photo_at(response_last.text or "")
                if oldest_last and (not oldest or oldest_last < oldest):
                    oldest = oldest_last

        info["oldest_photo_at"] = oldest
        info["estimated_years"] = estimate_years(oldest)
        return info

    def _wait(self, delay=None):
        if delay is None:
            delay = self.settings.get("discover_delay", 1.2)
        try:
            delay = max(0.0, float(delay))
        except (TypeError, ValueError):
            delay = 1.0
        # 分片休眠，保证停止/暂停能及时响应
        slept = 0.0
        while slept < delay:
            if not self.check_control():
                return
            step = min(0.2, delay - slept)
            time.sleep(step)
            slept += step

    # ---------- 列表页 / 搜索页（需要登录态） ----------

    # 列表页里常见的店铺卡片容器。命中就用它的文字做「名称/年限提示」，
    # 命不中也不影响：店铺 ID 只依赖 /shop/<id> 链接，名称最终以相册页为准。
    LIST_CARD_SELECTORS = (
        "#shop-all-list li",
        "div.shop-list li",
        "ul.shop-list li",
        "li.shop-item",
        "div.shop-item",
        "div.txt",
    )
    _SHOP_HREF_RE = re.compile(r"/shop/[A-Za-z0-9_-]{3,}")

    def fetch_list_page(self, url):
        """抓一个列表页 / 搜索页（需要登录态）。返回 (html, 错误说明)。"""
        response = self.http.get_html(url)
        if response is None:
            return "", "请求失败（网络错误或超时）"
        html = response.text or ""
        final_url = getattr(response, "url", "") or url
        if is_verify_page(html) or is_verify_page(final_url):
            return "", (
                "被点评风控拦截（跳到了验证中心）：Cookie 失效或未登录。"
                "请在已登录的浏览器里重新打开一次列表页，"
                "再用右键「Copy as cURL」复制新的登录态粘贴进来"
            )
        if response.status_code != 200:
            return "", f"HTTP {response.status_code}"
        if not html.strip():
            return "", "页面内容为空"
        return html, ""

    def shop_cards_from_list(self, html):
        """
        从列表页 HTML 提取 [(店铺ID, 名称提示, 年限提示)]。

        只依赖 `/shop/<id>` 链接，所以任何版式都能用；名称与年限只是页面提示，
        最终仍以相册页核验结果为准（沿用 V3.0 的结构化解析）。
        """
        cards = []
        seen = set()
        if not html:
            return cards

        if BS4_AVAILABLE:
            try:
                soup = BeautifulSoup(html, "html.parser")
            except Exception:
                soup = None
            if soup is not None:
                for selector in self.LIST_CARD_SELECTORS:
                    try:
                        nodes = soup.select(selector)
                    except Exception:
                        continue
                    if not nodes:
                        continue
                    for node in nodes:
                        # 一个卡片里通常有两个 /shop/ 链接：图片链接（没有文字）
                        # 和店名链接（<h4>店名</h4>）。要按「哪个有文字」来取名字。
                        match = None
                        name = ""
                        for anchor in node.find_all(
                            "a", href=self._SHOP_HREF_RE
                        ):
                            found = SHOP_ID_RE.search(anchor.get("href", ""))
                            if not found:
                                continue
                            if match is None:
                                match = found
                            if not name:
                                heading = anchor.find(["h4", "h3", "h2"])
                                for candidate in (
                                    heading.get_text(" ", strip=True)
                                    if heading is not None else "",
                                    anchor.get_text(" ", strip=True),
                                    anchor.get("title", ""),
                                ):
                                    cleaned = clean_display_name(
                                        candidate, max_len=60
                                    )
                                    if cleaned:
                                        name = cleaned
                                        break
                            if name:
                                break
                        if match is None:
                            continue
                        shop_id = match.group(1)
                        if (
                            shop_id in seen
                            or shop_id.lower() in _BAD_SHOP_IDS
                        ):
                            continue
                        if not name:
                            image = node.find("img")
                            if image is not None:
                                name = clean_display_name(
                                    image.get("alt") or image.get("title") or "",
                                    max_len=60,
                                )
                        seen.add(shop_id)
                        cards.append(
                            (shop_id, name, self._age_hint(self._card_text(node)))
                        )
                    if cards:
                        return cards

        for shop_id in extract_shop_ids(html):
            if shop_id in seen:
                continue
            seen.add(shop_id)
            cards.append((shop_id, "", ""))
        return cards

    @staticmethod
    def _card_text(node):
        try:
            text = node.get_text(" ", strip=True)
        except Exception:
            return ""
        return re.sub(r"\s+", " ", text or "")[:300]

    @staticmethod
    def _age_hint(text):
        """列表页上可能写着「15年老店」「老字号」，作为额外提示记录下来。"""
        if not text:
            return ""
        match = re.search(r"(\d{1,2})\s*年(?:老店|老字号|老铺|历史)", text)
        if match:
            return f"页面标注 {match.group(1)} 年老店（仅提示）"
        match = re.search(r"(百年老店|老字号|中华老字号)", text)
        if match:
            return f"页面标注「{match.group(1)}」（仅提示）"
        return ""

    def test_list_url(self, url):
        """探测列表页能不能访问（供界面上的「测试」按钮用）。"""
        if not str(url or "").strip():
            return False, "请先填写列表页网址"
        if not self.http.has_cookie:
            return False, (
                "还没填 Cookie。点评列表页有风控，需要先在已登录的浏览器里"
                "「Copy as cURL」再粘贴进来"
            )
        html, error = self.fetch_list_page(str(url).strip())
        if error:
            return False, error
        cards = self.shop_cards_from_list(html)
        return True, f"访问成功，本页解析到 {len(cards)} 家店铺"

    def discover_from_url(self, url, max_pages=3, source="列表页", **filters):
        """带登录态自动翻页找店：先收集店铺 ID，再逐店核验筛选。"""
        url = str(url or "").strip()
        empty_stats = {"total": 0, "checked": 0, "passed": 0,
                       "failed": 0, "reasons": {}, "error": ""}
        if not url:
            return [], empty_stats
        if not self.http.has_cookie:
            self.logger(
                "[DISCOVER] 未填写 Cookie：列表页有风控，基本会被拦。"
                "请在已登录的浏览器里右键 →「复制」→「以 cURL 格式复制」再粘贴。"
            )

        try:
            max_pages = max(1, int(max_pages))
        except (TypeError, ValueError):
            max_pages = 1

        shop_ids = []
        seen = set()
        hints = {}
        empty_pages = 0
        next_url = ""
        last_signature = ""
        for page in range(1, max_pages + 1):
            if not self.check_control():
                self.logger("[DISCOVER] 已停止")
                break
            # 优先跟页面自己给出的「下一页」链接（点评说写什么就写什么），
            # 只在没有该链接时才自己拼地址。
            page_url = next_url or list_page_url(url, page)
            html, error = self.fetch_list_page(page_url)
            if error:
                self.logger(f"[DISCOVER] 第 {page} 页失败：{page_url} → {error}")
                if page == 1:
                    stats = dict(empty_stats)
                    stats["error"] = error
                    return [], stats
                if "403" in str(error):
                    self.logger(
                        "[DISCOVER] 提示：翻页 403 是地址写法问题，不是 Cookie 问题——"
                        "只有一级分类时用 /p2，带排序/二级分类/商圈时页码必须紧贴上一段"
                        "（/o11p2、/g101p2）；另外 ?pg=2 虽然返回 200，"
                        "但内容还是第 1 页，不要用它。"
                    )
                break

            cards = self.shop_cards_from_list(html)
            signature = "|".join(shop_id for shop_id, _, _ in cards)
            if page > 1 and signature and signature == last_signature:
                self.logger(
                    "[DISCOVER] 本页店铺与上一页完全相同（点评忽略了翻页参数），"
                    "停止翻页以免重复采集"
                )
                break
            last_signature = signature

            new = 0
            for shop_id, name, age_hint in cards:
                if shop_id in seen:
                    continue
                seen.add(shop_id)
                shop_ids.append(shop_id)
                if name or age_hint:
                    hints[shop_id] = (name, age_hint)
                new += 1
            self.logger(
                f"[DISCOVER] 第 {page} 页：解析到 {len(cards)} 家，新增 {new} 家"
            )
            if new == 0:
                empty_pages += 1
                if empty_pages >= 2:
                    self.logger("[DISCOVER] 连续两页没有新店铺，停止翻页")
                    break
            else:
                empty_pages = 0

            next_url = find_next_page_url(html, page_url)
            if not next_url and page < max_pages and cards:
                # 页面没给出「下一页」链接（例如另存为的 HTML、或版式变了）：
                # 退回按地址规则拼；上面的「内容与上一页相同就停」会兜住空转。
                self.logger(
                    f"[DISCOVER] 第 {page} 页没有「下一页」链接，按地址规则继续翻页"
                )
            self._wait(filters.get("delay"))

        if not shop_ids:
            stats = dict(empty_stats)
            stats["error"] = "列表页里没解析到任何店铺链接"
            return [], stats

        self.logger(
            f"[DISCOVER] 列表页共收集 {len(shop_ids)} 家店铺，开始逐店核验"
        )
        return self.discover(
            shop_ids, source=source, hints=hints, **filters
        )

    def learn_filters(self, url=None, html=None, base_url=""):
        """
        学习列表页的「城市 / 分类 / 二级分类 / 商圈 / 排序」对应关系。
        给了 html 就用本地 HTML（离线，可用于浏览器另存为的页面），否则联网抓 url。
        返回 (learned, error)。
        """
        if html is None:
            if not url:
                return {}, "需要提供网址或 HTML"
            html, error = self.fetch_list_page(url)
            if error:
                return {}, error
        learned = learn_list_filters(html, base_url or url or "")
        total = sum(len(learned.get(key) or []) for key in (
            "categories", "groups", "regions", "sorts", "cities"
        ))
        if not total:
            return learned, "页面里没找到可学习的分类/排序链接（可能是搜索页或版式变了）"
        return learned, ""

    def resolve_category_sort(self, learned, category="", sort="", city=""):
        """
        把用户填的「美食 / ch10 / 10」解析成真实链接；解析不出来就按语法拼。
        返回 (category_url, sort_url)，排序链接优先用页面上学到的真实链接。
        """
        category_url = ""
        sort_url = ""
        sort_code = ""
        picked_category = find_learned_option(learned, "categories", category)
        picked_sort = find_learned_option(learned, "sorts", sort)
        if picked_category:
            category_url = picked_category.get("url", "")
            category_code = picked_category.get("code", "")
        else:
            category_code = _normalize_segment(category, "ch") or "ch10"
            category_url = build_list_url(city or "shanghai", category_code)

        if picked_sort:
            sort_url = picked_sort.get("url", "")
            sort_code = picked_sort.get("code", "")
        else:
            sort_code = _normalize_segment(sort, "o")

        if picked_category and sort_code and not picked_sort:
            sort_url = f"{category_url.rstrip('/')}/{sort_code}"
        if not sort_url:
            sort_url = build_list_url(
                city or "shanghai", category_code, sort=sort_code
            )
        if not category_url:
            category_url = build_list_url(city or "shanghai", category_code)
        return category_url, sort_url

    def discover_city(self, city, category="", sort="", max_pages=2,
                      max_categories=0, source="城市扫描", **filters):
        """
        大范围搜索：从 {城市} 的列表页里学出所有一级分类，然后逐分类翻页收集店铺，
        最后统一核验筛选（城市 / 经营年限 / 照片数）。

        category 可指定一个分类（如「美食」/ch10）；留空表示扫描学到的全部分类。
        """
        pinyin = city_pinyin(city)
        if not pinyin:
            return [], {"total": 0, "checked": 0, "passed": 0, "failed": 0,
                        "reasons": {},
                        "error": f"认不出城市「{city}」，请填中文名或拼音"}

        base_category = category or "ch10"
        start_url = build_list_url(
            pinyin, _normalize_segment(base_category, "ch") or "ch10", sort=sort
        )
        self.logger(f"[扫描] 城市 {city_label(pinyin) or pinyin}，入口 {start_url}")

        html, error = self.fetch_list_page(start_url)
        if error:
            return [], {"total": 0, "checked": 0, "passed": 0, "failed": 0,
                        "reasons": {}, "error": error}

        learned = learn_list_filters(html, start_url)
        category_options = list(learned.get("categories") or [])
        if sort:
            _, sort_url = self.resolve_category_sort(
                learned, base_category, sort, pinyin
            )
            category_options = [{
                "label": f"{base_category} · {sort}",
                "code": _normalize_segment(base_category, "ch") or "ch10",
                "url": sort_url,
            }]
        if not category_options:
            category_options = [{
                "label": base_category,
                "code": _normalize_segment(base_category, "ch") or "ch10",
                "url": start_url,
            }]

        self.logger(
            f"[扫描] 学到 {len(category_options)} 个一级分类、"
            f"{len(learned.get('sorts') or [])} 个排序方式"
        )
        if max_categories:
            try:
                category_options = category_options[: max(1, int(max_categories))]
            except (TypeError, ValueError):
                pass

        try:
            max_pages = max(1, int(max_pages))
        except (TypeError, ValueError):
            max_pages = 1

        shop_ids = []
        seen = set()
        hints = {}
        first_page_html = html   # 入口页已经抓过了，别重复请求
        for option in category_options:
            if not self.check_control():
                self.logger("[扫描] 已停止")
                break
            label = option.get("label") or option.get("code") or "分类"
            category_url = option.get("url") or start_url
            category_next_url = ""
            category_signature = ""
            for page in range(1, max_pages + 1):
                if not self.check_control():
                    break
                page_url = category_next_url or list_page_url(category_url, page)
                if page_url == start_url and first_page_html:
                    page_html, page_error = first_page_html, ""
                else:
                    page_html, page_error = self.fetch_list_page(page_url)
                if page_error:
                    self.logger(
                        f"[扫描] {label} 第 {page} 页失败：{page_url} → {page_error}"
                    )
                    if "403" in str(page_error) and page > 1:
                        self.logger(
                            "[扫描] 提示：带排序/二级分类/商圈时页码要紧贴上一段"
                            "（o11p2、g101p2），用 /o11/p2 会被 403。"
                        )
                    break
                cards = self.shop_cards_from_list(page_html)
                signature = "|".join(shop_id for shop_id, _, _ in cards)
                if page > 1 and signature and signature == category_signature:
                    self.logger(
                        f"[扫描] {label} 第 {page} 页与上一页相同（翻页被忽略），跳过"
                    )
                    break
                category_signature = signature
                new = 0
                for shop_id, name, age_hint in cards:
                    if shop_id in seen:
                        continue
                    seen.add(shop_id)
                    shop_ids.append(shop_id)
                    if name or age_hint:
                        hints[shop_id] = (name, age_hint)
                    new += 1
                self.logger(
                    f"[扫描] {label} 第 {page} 页：解析 {len(cards)} 家，新增 {new} 家"
                )
                if new == 0:
                    break
                category_next_url = find_next_page_url(page_html, page_url)
                self._wait(filters.get("delay"))

        if not shop_ids:
            return [], {"total": 0, "checked": 0, "passed": 0, "failed": 0,
                        "reasons": {}, "error": "扫描下来没有解析到任何店铺"}

        self.logger(f"[扫描] 共收集 {len(shop_ids)} 家店铺，开始逐店核验")
        return self.discover(shop_ids, source=source, hints=hints, **filters)

    # ---------- 筛选 ----------

    def judge(self, info, city_keyword="", min_years=0, min_photos=0):
        """返回 (是否通过, 未通过原因)。"""
        city_keyword = (city_keyword or "").strip()
        if city_keyword and city_keyword not in (info.get("city") or ""):
            return False, f"城市不符（{info.get('city') or '未知'}）"
        if min_photos and int(info.get("photo_count") or 0) < int(min_photos):
            return False, f"照片数 {info.get('photo_count') or 0} < {min_photos}"
        if min_years:
            years = info.get("estimated_years")
            if years is None:
                return False, "无照片日期，无法估算经营年限"
            if years < float(min_years):
                return False, (
                    f"最早照片 {info.get('oldest_photo_at') or '?'}"
                    f"（约 {years} 年）"
                )
        return True, ""

    # ---------- 发现 ----------

    def discover(self, shop_ids, source="", city_keyword="", min_years=0,
                 min_photos=0, limit=None, delay=None, update_shops=False,
                 hints=None):
        """
        逐店核验并按条件筛选。返回 (通过列表, 统计字典)。
        通过的结果会写入 candidates 表（status=candidate）。
        update_shops=True 时还会把店名/城市/照片数/最早照片写回 shops（用于「刷新档案库」）。
        hints 是列表页给的 {shop_id: (名称提示, 年限提示)}，只作为补充信息。
        """
        ids = []
        seen = set()
        for shop_id in shop_ids or []:
            shop_id = str(shop_id).strip()
            if shop_id and shop_id not in seen:
                seen.add(shop_id)
                ids.append(shop_id)

        if limit:
            ids = ids[: int(limit)]

        stats = {
            "total": len(ids), "checked": 0, "passed": 0,
            "failed": 0, "reasons": {},
        }
        results = []
        rate_limited_in_a_row = 0
        probed_years = []
        for index, shop_id in enumerate(ids, start=1):
            if not self.check_control():
                self.logger("[DISCOVER] 已停止")
                break
            info = self.probe_shop(shop_id, delay=delay)
            stats["checked"] += 1

            if info["error"]:
                stats["failed"] += 1
                stats["reasons"][info["error"]] = (
                    stats["reasons"].get(info["error"], 0) + 1
                )
                self.logger(f"[DISCOVER] {index}/{len(ids)} {shop_id} 跳过：{info['error']}")
                if re.search(r"\b(403|429)\b", info["error"]):
                    # 点评在限流：先自动放慢，连续太多就中止，避免白跑还把 IP 拖黑
                    stats["rate_limited"] = stats.get("rate_limited", 0) + 1
                    rate_limited_in_a_row += 1
                    if rate_limited_in_a_row == 1:
                        base = delay if delay else self.settings.get(
                            "discover_delay", 1.2
                        )
                        delay = max(float(base or 1.2) * 3, 3.0)
                        self.logger(
                            f"[DISCOVER] 检测到限流（HTTP 403/429），"
                            f"核验间隔自动提高到 {delay:.1f} 秒"
                        )
                    elif rate_limited_in_a_row >= 5:
                        stats["aborted"] = "rate_limited"
                        self.logger(
                            "[DISCOVER] 连续 5 家被 403/429 拦下：点评正在限流，"
                            "已中止本轮核验（已核验的结果仍然保留）。\n"
                            "  建议：等几分钟再试；把「核验间隔」调大到 3~5 秒；"
                            "「最多核验数」先设小一点。"
                        )
                        break
                else:
                    rate_limited_in_a_row = 0
            else:
                rate_limited_in_a_row = 0
                if not info["shop_name"] and hints:
                    # 相册页没给出名字时，用列表页上的名称提示兜底
                    info["shop_name"] = (hints.get(shop_id) or ("", ""))[0]
                if update_shops and self.db is not None:
                    self.db.update_shop_meta(
                        shop_id,
                        shop_name=info["shop_name"],
                        city=info["city"],
                        photo_count=info["photo_count"],
                        oldest_photo_at=info["oldest_photo_at"],
                    )
                ok, reason = self.judge(
                    info, city_keyword=city_keyword,
                    min_years=min_years, min_photos=min_photos,
                )
                if ok:
                    stats["passed"] += 1
                    results.append(info)
                    self.logger(
                        f"[DISCOVER] {index}/{len(ids)} ✓ {info['shop_name']}"
                        f"（{info['city']}）照片 {info['photo_count']} 张，"
                        f"最早 {info['oldest_photo_at'] or '?'}"
                        f"（约 {info['estimated_years']} 年）"
                    )
                    if self.db is not None:
                        note = ""
                        if hints:
                            name_hint, age_hint = hints.get(shop_id) or ("", "")
                            note = age_hint or ""
                        self.db.upsert_candidate(
                            shop_id=shop_id,
                            shop_name=info["shop_name"],
                            city=info["city"],
                            photo_count=info["photo_count"],
                            oldest_photo_at=info["oldest_photo_at"],
                            estimated_years=info["estimated_years"] or 0,
                            source=source,
                            status="candidate",
                            note=note,
                            url=info["url"],
                        )
                else:
                    stats["failed"] += 1
                    stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
                    self.logger(
                        f"[DISCOVER] {index}/{len(ids)} ✗ "
                        f"{info['shop_name'] or shop_id}：{reason}"
                    )
                if info.get("estimated_years") is not None:
                    probed_years.append(float(info["estimated_years"]))

            if self.progress:
                try:
                    self.progress(index, len(ids), info)
                except Exception:
                    pass
            self._wait(delay)

        # 年限分布诊断：点评相册里最早的照片通常只有几年，阈值设太高会一家都留不下
        if probed_years:
            probed_years.sort()
            median = probed_years[len(probed_years) // 2]
            stats["years_min"] = probed_years[0]
            stats["years_max"] = probed_years[-1]
            stats["years_median"] = median
            self.logger(
                f"[DISCOVER] 年限分布：本次核验到的店，最早照片距今 "
                f"{probed_years[0]:.1f} ~ {probed_years[-1]:.1f} 年"
                f"（中位 {median:.1f} 年）"
            )
            if min_years and not results and probed_years[-1] < float(min_years):
                suggested = max(1, int(probed_years[-1]))
                self.logger(
                    f"[DISCOVER] 提示：没有一家达到 {min_years} 年。"
                    f"相册里最早的照片通常只有 4~10 年，所以「经营年限」这个下界"
                    f"很难超过十几年。建议把「最少经营年限」调到 {suggested} 年左右，"
                    f"或者设为 0，改用「最少照片数」+「城市」来筛。"
                )
        return results, stats

    def discover_from_text(self, text, source="文本", **filters):
        """从任意文本（分享文本、复制的链接、列表页 HTML 源码）里找店并核验。"""
        ids = extract_shop_ids(text)
        self.logger(f"[DISCOVER] 文本里提取到 {len(ids)} 个店铺链接")
        if not ids:
            return [], {"total": 0, "checked": 0, "passed": 0,
                        "failed": 0, "reasons": {}}
        return self.discover(ids, source=source, **filters)

    def extract_shop_ids_from_files(self, paths):
        """从本地 HTML / 文本文件里提取所有店铺 ID（去重、保序）。"""
        ids = []
        seen = set()
        for path in paths or []:
            content = read_text_file(path)
            if content is None:
                self.logger(f"[DISCOVER] 无法读取文件：{path}")
                continue
            file_ids = extract_shop_ids(content)
            self.logger(
                f"[DISCOVER] {Path(path).name}：发现 {len(file_ids)} 个店铺链接"
            )
            for shop_id in file_ids:
                if shop_id not in seen:
                    seen.add(shop_id)
                    ids.append(shop_id)
        return ids

    def discover_from_files(self, paths, source="本地HTML", **filters):
        """从「浏览器另存为的列表页/搜索结果页」等本地文件里找店并核验。"""
        ids = self.extract_shop_ids_from_files(paths)
        if not ids:
            return [], {"total": 0, "checked": 0, "passed": 0,
                        "failed": 0, "reasons": {}}
        return self.discover(ids, source=source, **filters)

    def discover_from_archive(self, **filters):
        """
        核验档案库里已有的商户：把名称/城市/照片数/最早照片补齐，
        并按条件挑出「老店」。结果同样写入 candidates（标记为已采集）。
        """
        if self.db is None:
            return [], {"total": 0, "checked": 0, "passed": 0,
                        "failed": 0, "reasons": {}}
        shop_ids = [row[0] for row in self.db.get_shops()]
        self.logger(f"[DISCOVER] 档案库中共 {len(shop_ids)} 家商户，开始核验")
        results, stats = self.discover(
            shop_ids, source="档案库", update_shops=True, **filters
        )
        if self.db is not None:
            for shop_id in shop_ids:
                self.db.set_candidate_status(shop_id, "collected")
        return results, stats


# ============================================================
# 图片查看器
# ============================================================

class ImageViewer:

    def __init__(self, master, image_path, title="图片"):
        self.master = master
        self.window = tk.Toplevel(master)
        self.window.title(title)
        self.window.geometry("1100x800")
        self.window.configure(bg="black")
        self.original = None
        self.tk_image = None
        self.canvas = tk.Canvas(self.window, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.load_image(image_path)
        self.canvas.bind("<Configure>", self.on_resize)

    def load_image(self, path):
        try:
            self.original = Image.open(path)
            self.original = ImageOps.exif_transpose(self.original).convert("RGB")
            self.render()
        except Exception as e:
            messagebox.showerror("打开失败", str(e), parent=self.window)

    def render(self):
        if self.original is None:
            return
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w <= 1 or h <= 1:
            return
        image = self.original.copy()
        image.thumbnail(
            (max(100, w - 30), max(100, h - 30)),
            Image.Resampling.LANCZOS,
        )
        self.tk_image = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        self.canvas.create_image(
            w // 2, h // 2, image=self.tk_image, anchor="center"
        )

    def on_resize(self, event):
        self.render()


# ============================================================
# 档案浏览器
# ============================================================

class ArchiveBrowser:

    def __init__(self, parent, db_path, base_dir, settings):
        self.parent = parent
        self.db_path = db_path
        self.base_dir = base_dir
        self.settings = settings
        self.db = ArchiveDB(db_path)
        self.thumbnail_refs = []
        self.current_shop = None
        self.current_page = None
        self.create_ui()
        self.refresh_shops()

    def destroy(self):
        try:
            self.db.close()
        except Exception:
            pass

    def create_ui(self):
        main = ttk.Frame(self.parent)
        main.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(main, width=280)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        ttk.Label(
            left, text="档案库",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        self.search_var = tk.StringVar()
        search_frame = ttk.Frame(left)
        search_frame.pack(fill="x", pady=(0, 6))
        ttk.Entry(search_frame, textvariable=self.search_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            search_frame, text="搜索",
            command=self.refresh_shops, width=6,
        ).pack(side="left", padx=(5, 0))

        self.shop_tree = ttk.Treeview(left, show="tree")
        self.shop_tree.column("#0", width=260, minwidth=180, stretch=False)
        tree_scroll = ttk.Scrollbar(
            left, orient="vertical", command=self.shop_tree.yview
        )
        self.shop_tree.configure(yscrollcommand=tree_scroll.set)
        self.shop_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.shop_tree.bind("<<TreeviewSelect>>", self.on_shop_select)

        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        self.info_label = ttk.Label(right, text="请选择商户", anchor="w")
        self.info_label.pack(fill="x", pady=(0, 6))

        body = ttk.PanedWindow(right, orient="horizontal")
        body.pack(fill="both", expand=True)

        page_frame = ttk.Frame(body, width=150)
        body.add(page_frame, weight=0)
        ttk.Label(page_frame, text="页面").pack(anchor="w")
        self.page_list = tk.Listbox(page_frame, width=12)
        page_scroll = ttk.Scrollbar(
            page_frame, orient="vertical", command=self.page_list.yview
        )
        self.page_list.configure(yscrollcommand=page_scroll.set)
        self.page_list.pack(side="left", fill="both", expand=True)
        page_scroll.pack(side="right", fill="y")
        self.page_list.bind("<<ListboxSelect>>", self.on_page_select)

        image_frame = ttk.Frame(body)
        body.add(image_frame, weight=1)
        self.image_canvas = tk.Canvas(
            image_frame, background="#eeeeee", highlightthickness=0
        )
        self.image_scroll = ttk.Scrollbar(
            image_frame, orient="vertical", command=self.image_canvas.yview
        )
        self.image_canvas.configure(yscrollcommand=self.image_scroll.set)
        self.image_canvas.pack(side="left", fill="both", expand=True)
        self.image_scroll.pack(side="right", fill="y")

        self.thumb_frame = ttk.Frame(self.image_canvas)
        self.canvas_window = self.image_canvas.create_window(
            (0, 0), window=self.thumb_frame, anchor="nw"
        )
        self.thumb_frame.bind("<Configure>", self.on_thumb_configure)

        self.stats_label = ttk.Label(right, text="", anchor="w")
        self.stats_label.pack(fill="x", pady=(6, 0))

        # 鼠标悬停在缩略图说明上时显示原始时间/图片ID/来源/置信度
        self.tip_label = ttk.Label(
            right, text="", anchor="w", foreground="#555",
        )
        self.tip_label.pack(fill="x", pady=(2, 0))

    def refresh_shops(self):
        for item in self.shop_tree.get_children():
            self.shop_tree.delete(item)
        keyword = self.search_var.get().strip().lower()
        shops = self.db.get_shops()
        city_nodes = {}

        for (shop_id, shop_name, city, page_count, image_count,
             photo_count, oldest_photo_at) in shops:
            display = f"{shop_name or '未命名商户'} [{shop_id}]"
            years = estimate_years(oldest_photo_at)
            if years is not None:
                display += f" · 至少{years:.0f}年"
            searchable = f"{shop_id} {shop_name} {city}".lower()
            if keyword and keyword not in searchable:
                continue
            city_name = city if city else "未分类城市"
            if city_name not in city_nodes:
                city_nodes[city_name] = self.shop_tree.insert(
                    "", "end", text=city_name, open=True
                )
            node = self.shop_tree.insert(
                city_nodes[city_name], "end", text=display,
                values=(shop_id, page_count, image_count),
            )
            self.shop_tree.item(node, tags=(shop_id,))

        stats = self.db.stats()
        self.stats_label.config(
            text=(
                f"商户 {stats['shops']}  |  "
                f"页面 {stats['pages']}  |  "
                f"图片 {stats['images']}  |  "
                f"唯一图片 {stats['unique_images']}  |  "
                f"重复 {stats['duplicates']}  |  "
                f"有作者 {stats.get('with_uploader', 0)}  |  "
                f"有时间 {stats.get('with_published', 0)}  |  "
                f"年份推算 {stats.get('year_inferred', 0)}"
            )
        )

    def on_shop_select(self, event=None):
        selected = self.shop_tree.selection()
        if not selected:
            return
        tags = self.shop_tree.item(selected[0], "tags")
        if not tags:
            return
        shop_id = tags[0]
        self.current_shop = shop_id
        shop = self.db.get_shop(shop_id)
        if shop:
            (shop_id, shop_name, city, source_url, resolved_url,
             photo_count, oldest_photo_at) = shop
            detail = f"{city or '未知城市'}  |  {shop_name or '未命名商户'}"
            if photo_count:
                detail += f"  |  相册 {photo_count} 张"
            if oldest_photo_at:
                years = estimate_years(oldest_photo_at)
                detail += f"  |  最早照片 {oldest_photo_at}"
                if years is not None:
                    detail += f"（至少 {years:.0f} 年）"
            detail += f"  |  Shop ID: {shop_id}"
            self.info_label.config(text=detail)
        self.page_list.delete(0, "end")
        for row in self.db.get_pages(shop_id):
            self.page_list.insert("end", f"第 {row[0]} 页")
        self.clear_thumbnails()

    def on_page_select(self, event=None):
        if not self.current_shop:
            return
        selection = self.page_list.curselection()
        if not selection:
            return
        index = selection[0]
        pages = self.db.get_pages(self.current_shop)
        if index >= len(pages):
            return
        page_no = pages[index][0]
        self.current_page = page_no
        self.show_images(self.current_shop, page_no)

    def clear_thumbnails(self):
        for child in self.thumb_frame.winfo_children():
            child.destroy()
        self.thumbnail_refs.clear()

    def show_images(self, shop_id, page_no):
        self.clear_thumbnails()
        try:
            self.tip_label.config(text="")
        except Exception:
            pass
        images = self.db.get_images(shop_id, page_no)
        row = 0
        col = 0

        thumb_w = self.settings.get("thumb_w", 150)
        thumb_h = self.settings.get("thumb_h", 120)

        for image_row in images:
            (image_id, page, image_url, local_path, sha, phash,
             width, height, filesize, uploader, published_at,
             metadata_source, duplicate_of, status,
             photo_id, photo_index, published_raw,
             year_inferred, metadata_confidence) = image_row
            if not local_path:
                continue
            path = Path(local_path)
            if not path.exists():
                continue
            try:
                image = Image.open(path)
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                tk_img = ImageTk.PhotoImage(image)
                self.thumbnail_refs.append(tk_img)

                card = ttk.Frame(
                    self.thumb_frame, relief="solid", borderwidth=1
                )
                card.grid(row=row, column=col, padx=5, pady=5, sticky="n")

                label = tk.Label(card, image=tk_img, bg="white")
                label.pack(padx=3, pady=3)

                name = path.name
                uploader_text = uploader or "未知作者"
                date_text = published_at or "未知时间"
                if year_inferred and published_at:
                    # 年份是按「当年显示 MM-DD」规则推定的，标注出来便于核对
                    date_text = f"{published_at}＊"
                caption = (
                    f"{uploader_text[:18]}\n"
                    f"{date_text}\n"
                    f"{width}×{height}"
                )
                text_label = ttk.Label(
                    card, text=caption, width=22, anchor="center"
                )
                text_label.pack(padx=3, pady=(0, 3))
                if published_raw or photo_id:
                    detail = "  ".join(
                        part for part in (
                            f"原始：{published_raw}" if published_raw else "",
                            f"图片ID：{photo_id}" if photo_id else "",
                            f"来源：{metadata_source}" if metadata_source else "",
                            f"置信度：{metadata_confidence}"
                            if metadata_confidence else "",
                        ) if part
                    )
                    text_label.bind(
                        "<Enter>",
                        lambda e, tip=detail: self.tip_label.config(text=tip),
                    )
                    text_label.bind(
                        "<Leave>",
                        lambda e: self.tip_label.config(text=""),
                    )

                label.bind(
                    "<Button-1>",
                    lambda e, p=str(path), iid=image_id:
                    self.open_image(p, iid),
                )
                text_label.bind(
                    "<Button-1>",
                    lambda e, p=str(path), iid=image_id:
                    self.open_image(p, iid),
                )

                col += 1
                if col >= 5:
                    col = 0
                    row += 1
            except Exception:
                continue

        self.thumb_frame.update_idletasks()
        self.image_canvas.configure(
            scrollregion=(
                0, 0,
                self.thumb_frame.winfo_reqwidth(),
                self.thumb_frame.winfo_reqheight(),
            )
        )

    def open_image(self, path, image_id):
        ImageViewer(self.parent, path, title=f"Image #{image_id}")

    def on_thumb_configure(self, event=None):
        self.image_canvas.configure(
            scrollregion=self.image_canvas.bbox("all")
        )


# ============================================================
# 设置标签页
# ============================================================

class SettingsTab:

    def __init__(self, parent, settings, on_save_callback=None):
        self.parent = parent
        self.settings = settings
        self.on_save_callback = on_save_callback

        self.vars = {}

        self.build_ui()
        self.load_from_settings()

    def build_ui(self):
        # 主框架，带滚动
        outer = ttk.Frame(self.parent)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            outer, orient="vertical", command=canvas.yview
        )
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        inner = ttk.Frame(canvas)
        canvas_window = canvas.create_window(
            (0, 0), window=inner, anchor="nw"
        )

        def on_inner_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        inner.bind("<Configure>", on_inner_configure)

        def on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)

        canvas.bind("<Configure>", on_canvas_configure)

        # 鼠标滚轮支持
        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", on_mousewheel)

        # ============================================================
        # 分组 1：采集延迟
        # ============================================================
        g1 = ttk.LabelFrame(inner, text="采集延迟与超时", padding=10)
        g1.pack(fill="x", padx=10, pady=(10, 5))

        self.add_float_field(
            g1, "page_delay", "页面延迟（秒）", 0,
            "每页之间等待时间。风控严可调大到 2~3 秒"
        )
        self.add_float_field(
            g1, "image_delay", "图片延迟（秒）", 1,
            "每张图片之间等待时间"
        )
        self.add_int_field(
            g1, "request_timeout", "请求超时（秒）", 2,
            "HTML 请求超时时间"
        )
        self.add_int_field(
            g1, "image_timeout", "图片超时（秒）", 3,
            "图片下载超时时间"
        )

        # ============================================================
        # 分组 2：页面探测
        # ============================================================
        g2 = ttk.LabelFrame(inner, text="页面探测", padding=10)
        g2.pack(fill="x", padx=10, pady=5)

        self.add_int_field(
            g2, "max_page_discovery", "最大探测页数", 0,
            "二分搜索的安全上限"
        )
        self.add_int_field(
            g2, "verify_empty_limit", "最终验证页数", 1,
            "最后页后验证几个空页"
        )
        self.add_int_field(
            g2, "min_images_per_valid_page", "有效页最小图片数", 2,
            "低于此值的页面视为无效（正常页 16 张）"
        )
        self.add_int_field(
            g2, "photos_per_page", "每页图片数", 3,
            "用于「图片数量 → 最后一页」换算，大众点评为 16"
        )
        self.add_int_field(
            g2, "max_images_per_page", "每页最大下载数", 4,
            "安全上限，防止异常页面造成巨量下载"
        )

        # ============================================================
        # 分组 3：去重强化（开关）
        # ============================================================
        g3 = ttk.LabelFrame(inner, text="去重强化开关", padding=10)
        g3.pack(fill="x", padx=10, pady=5)

        self.add_bool_field(
            g3, "enable_in_page_dedup", "页内 URL 去重",
            "同一页面内 URL 相同的只处理一次"
        )
        self.add_bool_field(
            g3, "enable_url_dedup", "跨页 URL 预去重",
            "下载前查数据库，已存在则跳过（大幅减少请求）"
        )
        self.add_bool_field(
            g3, "enable_size_filter", "图片尺寸过滤",
            "小于最小边长的缩略图直接跳过"
        )
        self.add_bool_field(
            g3, "enable_sha256_dedup", "SHA256 精确去重",
            "字节完全相同的图片识别为重复"
        )
        self.add_bool_field(
            g3, "enable_phash_dedup", "pHash 相似去重",
            "视觉相似的图片识别为重复"
        )
        self.add_bool_field(
            g3, "enable_replace_higher", "更高分辨率替换",
            "发现更清晰的版本时替换旧文件（会删除旧文件）"
        )

        # ============================================================
        # 分组 4：阈值
        # ============================================================
        g4 = ttk.LabelFrame(inner, text="尺寸与阈值", padding=10)
        g4.pack(fill="x", padx=10, pady=5)

        self.add_int_field(
            g4, "min_image_dimension", "最小图片边长（px）", 0,
            "小于此边长的图片跳过。推荐 150~300"
        )
        self.add_int_field(
            g4, "phash_threshold", "pHash 相似阈值（0-16）", 1,
            "越小越严格。4 严格，8 宽松"
        )

        # ============================================================
        # 分组 5：缩略图
        # ============================================================
        g5 = ttk.LabelFrame(inner, text="缩略图显示", padding=10)
        g5.pack(fill="x", padx=10, pady=5)

        self.add_int_field(
            g5, "thumb_w", "缩略图宽", 0, "档案浏览界面缩略图宽度"
        )
        self.add_int_field(
            g5, "thumb_h", "缩略图高", 1, "档案浏览界面缩略图高度"
        )

        # ============================================================
        # 分组 6：上传者 / 发布时间
        # ============================================================
        g6 = ttk.LabelFrame(inner, text="上传者 / 发布时间", padding=10)
        g6.pack(fill="x", padx=10, pady=5)

        self.add_bool_field(
            g6, "infer_year_for_short_date", "补全 MM-DD 的年份",
            "点评「当年」照片只显示 06-21，「跨年」才显示 25-12-18；"
            "开启后按抓取当年补成 2026-06-21，并保留原始文本"
        )
        self.add_bool_field(
            g6, "use_original_image_url", "下载原图（不用 240×180 缩略图）",
            "相册卡片里的 <img> 只有 240×180（低于最小边长会被整批过滤）。"
            "开启后优先取页面内嵌的原图地址，失败自动回退"
        )
        self.add_bool_field(
            g6, "recrawl_completed_pages", "重采已完成页面以修正旧数据",
            "用于修复旧版本写错的作者/时间：配合「跨页 URL 预去重」开启时"
            "不重新下载图片，只重写元数据"
        )

        # ============================================================
        # 分组 7：采集日期范围
        # ============================================================
        g7 = ttk.LabelFrame(inner, text="采集日期范围", padding=10)
        g7.pack(fill="x", padx=10, pady=5)

        self.add_text_field(
            g7, "date_from", "起始日期（含）", 0,
            "留空表示不限。可以只写年份：2010 / 2010-06 / 2010-06-15"
        )
        self.add_text_field(
            g7, "date_to", "截止日期（含）", 1,
            "留空表示不限。只收集这个日期之前的照片"
        )
        self.add_bool_field(
            g7, "date_filter_keep_unknown", "日期未知的图片也收集",
            "页面没给出日期时保留（关掉则只收严格落在范围内的）。"
            "建议同时开启上面的「补全 MM-DD 的年份」",
            use_grid=True, row=2,
        )

        # ============================================================
        # 分组 8：自动找店
        # ============================================================
        g8 = ttk.LabelFrame(inner, text="自动找店条件", padding=10)
        g8.pack(fill="x", padx=10, pady=5)

        self.add_text_field(
            g8, "discover_city_keyword", "城市关键词", 0,
            "例如「上海」。按相册页标题里的城市过滤，留空表示不限"
        )
        self.add_int_field(
            g8, "discover_min_years", "最少经营年限", 1,
            "按「相册里最早的照片」估算，属于保守下界（真实经营只会更久）"
        )
        self.add_int_field(
            g8, "discover_min_photos", "最少照片数", 2,
            "相册照片总数下限，用来过滤太冷清的店"
        )
        self.add_int_field(
            g8, "discover_limit", "最多核验店铺数", 3,
            "每个候选店铺需要 2 次请求，别设太大"
        )
        self.add_float_field(
            g8, "discover_delay", "核验间隔（秒）", 4,
            "每个候选店铺之间的等待时间，防触发风控"
        )

        # ============================================================
        # 按钮
        # ============================================================
        btn_frame = ttk.Frame(inner)
        btn_frame.pack(fill="x", padx=10, pady=15)

        ttk.Button(
            btn_frame, text="保存设置",
            command=self.save_settings,
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_frame, text="恢复默认",
            command=self.reset_defaults,
        ).pack(side="left", padx=8)

        ttk.Button(
            btn_frame, text="重载文件",
            command=self.reload_from_file,
        ).pack(side="left", padx=8)

        # 状态提示
        self.status_label = ttk.Label(inner, text="", foreground="green")
        self.status_label.pack(fill="x", padx=10, pady=(0, 10))

    def add_int_field(self, parent, key, label, row, tooltip=""):
        ttk.Label(parent, text=label + "：").grid(
            row=row, column=0, sticky="w", pady=3
        )
        var = tk.StringVar()
        entry = ttk.Entry(parent, textvariable=var, width=15)
        entry.grid(row=row, column=1, sticky="w", padx=(0, 10), pady=3)
        if tooltip:
            ttk.Label(
                parent, text=tooltip, foreground="#888",
                font=("", 8),
            ).grid(row=row, column=2, sticky="w", pady=3)
        self.vars[key] = ("int", var)
        return entry

    def add_float_field(self, parent, key, label, row, tooltip=""):
        ttk.Label(parent, text=label + "：").grid(
            row=row, column=0, sticky="w", pady=3
        )
        var = tk.StringVar()
        entry = ttk.Entry(parent, textvariable=var, width=15)
        entry.grid(row=row, column=1, sticky="w", padx=(0, 10), pady=3)
        if tooltip:
            ttk.Label(
                parent, text=tooltip, foreground="#888",
                font=("", 8),
            ).grid(row=row, column=2, sticky="w", pady=3)
        self.vars[key] = ("float", var)
        return entry

    def add_text_field(self, parent, key, label, row, tooltip=""):
        ttk.Label(parent, text=label + "：").grid(
            row=row, column=0, sticky="w", pady=3
        )
        var = tk.StringVar()
        entry = ttk.Entry(parent, textvariable=var, width=15)
        entry.grid(row=row, column=1, sticky="w", padx=(0, 10), pady=3)
        if tooltip:
            ttk.Label(
                parent, text=tooltip, foreground="#888",
                font=("", 8),
            ).grid(row=row, column=2, sticky="w", pady=3)
        self.vars[key] = ("str", var)
        return entry

    def add_bool_field(self, parent, key, label, tooltip="",
                       use_grid=False, row=0):
        var = tk.BooleanVar()
        cb = ttk.Checkbutton(parent, text=label, variable=var)
        if use_grid:
            # 同一个容器里既有 grid 又有 pack 会报 TclError，所以按需切换
            cb.grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
            if tooltip:
                ttk.Label(
                    parent, text="    " + tooltip,
                    foreground="#888", font=("", 8),
                ).grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=(0, 4))
        else:
            cb.pack(anchor="w", pady=2)
            if tooltip:
                ttk.Label(
                    parent, text="    " + tooltip,
                    foreground="#888", font=("", 8),
                ).pack(anchor="w", pady=(0, 4))
        self.vars[key] = ("bool", var)
        return cb

    def load_from_settings(self):
        for key, (typ, var) in self.vars.items():
            value = self.settings.get(key, DEFAULT_SETTINGS.get(key))
            if typ == "bool":
                var.set(bool(value))
            else:
                var.set(str(value))

    def save_settings(self):
        errors = []
        for key, (typ, var) in self.vars.items():
            raw = var.get()
            try:
                if typ == "bool":
                    self.settings.set(key, bool(raw))
                elif typ == "int":
                    self.settings.set(key, int(raw))
                elif typ == "float":
                    self.settings.set(key, float(raw))
                else:
                    text = str(raw).strip()
                    if key in ("date_from", "date_to") and text:
                        normalized = normalize_date_bound(text)
                        if not normalized:
                            raise ValueError(
                                f"日期「{text}」无法识别，请用 "
                                f"2010 / 2010-06 / 2010-06-15 这样的写法"
                            )
                        text = normalized
                    self.settings.set(key, text)
            except Exception as exc:
                errors.append(f"{key}: {exc}")

        if errors:
            messagebox.showerror(
                "保存失败",
                "以下字段格式不正确：\n" + "\n".join(errors),
            )
            return

        ok = self.settings.save()
        if ok:
            self.status_label.config(
                text=f"✓ 已保存到 {self.settings.path.name}"
                f"  ({time.strftime('%H:%M:%S')})",
                foreground="green",
            )
            if self.on_save_callback:
                self.on_save_callback()
        else:
            self.status_label.config(
                text="✗ 保存失败，请检查磁盘权限",
                foreground="red",
            )

    def reset_defaults(self):
        if not messagebox.askyesno(
            "恢复默认",
            "将所有参数恢复为默认值？\n（不会自动保存，需要点击「保存设置」）",
        ):
            return
        for key, (typ, var) in self.vars.items():
            default_val = DEFAULT_SETTINGS.get(key)
            if typ == "bool":
                var.set(bool(default_val))
            else:
                var.set(str(default_val))
        self.status_label.config(
            text="已恢复默认值，请点击「保存设置」生效",
            foreground="blue",
        )

    def reload_from_file(self):
        self.settings.load()
        self.load_from_settings()
        self.status_label.config(
            text="已从文件重新加载",
            foreground="blue",
        )


# ============================================================
# 主 GUI
# ============================================================

class DianpingArchiveGUI:

    def __init__(self, root, settings):
        self.root = root
        self.settings = settings

        self.root.title(APP_NAME)
        self.root.geometry("1300x900")
        self.root.minsize(1000, 700)

        self.http = HTTPClient(settings)
        self.resolver = DianpingURLResolver(self.http)

        self.base_dir = tk.StringVar(
            value=str(Path.cwd() / "dianping_archive")
        )
        self.db = None
        self.controller = None
        self.log_queue = queue.Queue()
        self.discover_queue = queue.Queue()
        self.discover_thread = None
        self.discover_pause = threading.Event()
        self.discover_stop = threading.Event()
        self.discovery = None

        self.create_ui()
        self.root.after(100, self.flush_logs)
        self.ensure_database()

    def create_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)

        self.collect_tab = ttk.Frame(notebook)
        notebook.add(self.collect_tab, text="采集")
        self.create_collect_ui()

        self.discover_tab = ttk.Frame(notebook)
        notebook.add(self.discover_tab, text="自动找店")
        self.create_discover_ui()

        self.browser_tab = ttk.Frame(notebook)
        notebook.add(self.browser_tab, text="档案浏览")
        self.create_browser()

        self.settings_tab = ttk.Frame(notebook)
        notebook.add(self.settings_tab, text="设置")
        self.settings_ui = SettingsTab(
            self.settings_tab,
            self.settings,
            on_save_callback=self.on_settings_saved,
        )

    def on_settings_saved(self):
        """设置保存后的回调"""
        self.log("[SETTINGS] 设置已更新")

    def create_collect_ui(self):
        root_frame = ttk.Frame(self.collect_tab)
        root_frame.pack(fill="both", expand=True, padx=12, pady=12)

        ttk.Label(
            root_frame, text="大众点评链接 / 分享文本",
        ).grid(row=0, column=0, sticky="w", pady=5)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(root_frame, textvariable=self.url_var)
        self.url_entry.grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=8, pady=5
        )

        ttk.Label(root_frame, text="保存目录").grid(
            row=1, column=0, sticky="w", pady=5
        )
        ttk.Entry(root_frame, textvariable=self.base_dir).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=5
        )
        ttk.Button(root_frame, text="选择目录", command=self.choose_dir).grid(
            row=1, column=3, padx=5, pady=5
        )

        ttk.Label(root_frame, text="采集范围").grid(
            row=2, column=0, sticky="w", pady=5
        )
        self.mode_var = tk.StringVar(value="auto_all")
        mode_frame = ttk.Frame(root_frame)
        mode_frame.grid(row=2, column=1, columnspan=3, sticky="w", padx=8)

        ttk.Radiobutton(
            mode_frame, text="自动最后页 → 第 1 页",
            variable=self.mode_var, value="auto_all",
            command=self.update_range_state,
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            mode_frame, text="自动最后页 → 指定页",
            variable=self.mode_var, value="auto_to",
            command=self.update_range_state,
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            mode_frame, text="图片数量 → 第 1 页",
            variable=self.mode_var, value="count_all",
            command=self.update_range_state,
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            mode_frame, text="图片数量 → 指定页",
            variable=self.mode_var, value="count_to",
            command=self.update_range_state,
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            mode_frame, text="自定义范围",
            variable=self.mode_var, value="custom",
            command=self.update_range_state,
        ).pack(side="left")

        ttk.Label(root_frame, text="图片数量").grid(
            row=3, column=0, sticky="w", pady=5
        )
        count_frame = ttk.Frame(root_frame)
        count_frame.grid(row=3, column=1, columnspan=3, sticky="w", padx=8)
        self.photo_count_var = tk.StringVar(value="")
        self.photo_count_entry = ttk.Entry(
            count_frame, width=10, textvariable=self.photo_count_var
        )
        self.photo_count_entry.pack(side="left", padx=(0, 8))
        self.photo_count_hint = ttk.Label(count_frame, text="")
        self.photo_count_hint.pack(side="left")
        self.refresh_photo_count_hint()

        ttk.Label(root_frame, text="指定页码").grid(
            row=4, column=0, sticky="w", pady=5
        )
        page_frame = ttk.Frame(root_frame)
        page_frame.grid(row=4, column=1, columnspan=3, sticky="w", padx=8)
        ttk.Label(page_frame, text="起始/最后页").pack(side="left")
        self.start_var = tk.StringVar(value="")
        self.start_entry = ttk.Entry(
            page_frame, width=10, textvariable=self.start_var
        )
        self.start_entry.pack(side="left", padx=(6, 20))
        ttk.Label(page_frame, text="终止页").pack(side="left")
        self.stop_var = tk.StringVar(value="1")
        self.stop_entry = ttk.Entry(
            page_frame, width=10, textvariable=self.stop_var
        )
        self.stop_entry.pack(side="left", padx=6)

        button_frame = ttk.Frame(root_frame)
        button_frame.grid(
            row=5, column=0, columnspan=4, sticky="w", pady=15
        )
        self.start_button = ttk.Button(
            button_frame, text="开始采集", command=self.start_crawl
        )
        self.start_button.pack(side="left", padx=(0, 8))
        self.pause_button = ttk.Button(
            button_frame, text="暂停", command=self.pause_crawl
        )
        self.pause_button.pack(side="left", padx=8)
        self.resume_button = ttk.Button(
            button_frame, text="继续", command=self.resume_crawl
        )
        self.resume_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(
            button_frame, text="停止", command=self.stop_crawl
        )
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(
            button_frame, text="刷新档案库", command=self.refresh_browser
        ).pack(side="left", padx=8)
        ttk.Button(
            button_frame, text="清理重复文件", command=self.cleanup_duplicates
        ).pack(side="left", padx=8)

        self.progress = ttk.Progressbar(root_frame, mode="indeterminate")
        self.progress.grid(
            row=6, column=0, columnspan=4, sticky="ew", pady=5
        )

        ttk.Label(root_frame, text="运行日志").grid(
            row=7, column=0, sticky="w", pady=(10, 5)
        )
        log_frame = ttk.Frame(root_frame)
        log_frame.grid(row=8, column=0, columnspan=4, sticky="nsew")

        self.log_text = tk.Text(
            log_frame, wrap="none", height=25, font=("Consolas", 9)
        )
        log_scroll = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        root_frame.columnconfigure(1, weight=1)
        root_frame.columnconfigure(2, weight=1)
        root_frame.rowconfigure(8, weight=1)
        self.update_range_state()

    def refresh_photo_count_hint(self):
        per_page = self.settings.get("photos_per_page", 16)
        try:
            self.photo_count_hint.config(
                text=f"每页 {per_page} 张，会自动换算最后一页"
            )
        except Exception:
            pass

    # ============================================================
    # 自动找店
    # ============================================================

    def create_discover_ui(self):
        frame = ttk.Frame(self.discover_tab)
        frame.pack(fill="both", expand=True, padx=12, pady=10)

        ttk.Label(
            frame,
            text=(
                "点评的「分类列表页 / 搜索结果页 / 商户主页」有风控，匿名访问会跳到验证中心。"
                "已登录的浏览器能正常打开这些页面，所以这里有两种用法：\n"
                "① 推荐：在已登录的浏览器里打开列表页 →「Copy as cURL」→ 粘贴到下面 →"
                "填上网址，脚本会自动翻页、逐店核验、按城市与经营年限筛选；\n"
                "② 或者：把列表页 Ctrl+S 另存为 HTML（或直接复制链接文本）再导入，不需要登录态。\n"
                "核验一家店只需 2 次请求：相册首页拿到店名/城市/照片总数，"
                "末页拿到最早的照片 → 估算「至少经营了多少年」。"
            ),
            foreground="#555", wraplength=980, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        ttk.Label(frame, text="店铺来源").grid(row=1, column=0, sticky="w")
        source_frame = ttk.Frame(frame)
        source_frame.grid(row=1, column=1, columnspan=3, sticky="w")
        self.discover_source = tk.StringVar(value="url")
        for text, value in (
            ("列表页网址（需登录态，能自动翻页）", "url"),
            ("本地 HTML 文件（浏览器另存为）", "files"),
            ("粘贴文本 / 链接", "text"),
            ("档案库已有商户", "archive"),
        ):
            ttk.Radiobutton(
                source_frame, text=text,
                variable=self.discover_source, value=value,
            ).pack(side="left", padx=(0, 14))

        self.discover_paths = []
        self.discover_path_label = ttk.Label(
            frame, text="未选择文件", foreground="#888"
        )
        ttk.Button(
            frame, text="选择 HTML 文件…", command=self.choose_discover_files
        ).grid(row=2, column=1, sticky="w", pady=4)
        self.discover_path_label.grid(
            row=2, column=2, columnspan=2, sticky="w", padx=8
        )

        ttk.Label(frame, text="粘贴文本").grid(row=3, column=0, sticky="nw")
        self.discover_text = tk.Text(frame, height=4, wrap="none")
        self.discover_text.grid(
            row=3, column=1, columnspan=3, sticky="ew", pady=4
        )

        # 列表页网址 + 登录态：点评的列表页/搜索页有风控，必须带上浏览器里的登录态
        url_frame = ttk.LabelFrame(
            frame,
            text="列表页网址（自动翻页；列表页有风控，需要带上登录态）",
            padding=8,
        )
        url_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=4)

        self.discover_url = tk.StringVar(
            value=str(self.settings.get("list_url", ""))
        )
        self.discover_max_pages = tk.StringVar(
            value=str(self.settings.get("list_max_pages", 3))
        )
        ttk.Label(url_frame, text="网址：").grid(row=0, column=0, sticky="w")
        ttk.Entry(url_frame, textvariable=self.discover_url).grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=(0, 10)
        )
        ttk.Label(url_frame, text="最多翻页：").grid(row=0, column=3, sticky="w")
        ttk.Entry(
            url_frame, textvariable=self.discover_max_pages, width=5
        ).grid(row=0, column=4, sticky="w")

        ttk.Label(
            url_frame,
            text=(
                "登录态：已登录的浏览器里打开列表页 → 右键 →「复制」→"
                "「以 cURL 格式复制」→ 粘贴到下面 → 点「解析 cURL」"
            ),
            foreground="#555", font=("", 8),
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.discover_cookie_text = tk.Text(url_frame, height=3, wrap="none")
        self.discover_cookie_text.grid(
            row=2, column=0, columnspan=5, sticky="ew", pady=2
        )
        cookie_buttons = ttk.Frame(url_frame)
        cookie_buttons.grid(row=3, column=0, columnspan=5, sticky="w")
        ttk.Button(
            cookie_buttons, text="解析 cURL", command=self.parse_cookie_input
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            cookie_buttons, text="测试列表页", command=self.test_list_url
        ).pack(side="left", padx=8)
        ttk.Button(
            cookie_buttons, text="清除登录态", command=self.clear_cookie_input
        ).pack(side="left", padx=8)
        self.cookie_status_label = ttk.Label(
            cookie_buttons, text="", foreground="#888"
        )
        self.cookie_status_label.pack(side="left", padx=8)
        url_frame.columnconfigure(1, weight=1)
        self._refresh_cookie_status()

        # 地址生成器：学会「分类 / 排序」的对应关系后，可以一键拼出任意城市的列表页地址
        self.learned_filters = dict(
            self.settings.get("learned_filters", {}) or {}
        )
        generator = ttk.LabelFrame(
            frame, text="列表页地址生成器（先从页面学一次，再随便换城市）",
            padding=8,
        )
        generator.grid(row=3, column=4, rowspan=2, sticky="nsew", padx=(8, 0))
        self.gen_city = tk.StringVar(value="上海")
        self.gen_category = tk.StringVar()
        self.gen_sort = tk.StringVar()
        ttk.Label(generator, text="城市").grid(row=0, column=0, sticky="w")
        ttk.Entry(generator, textvariable=self.gen_city, width=10).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(generator, text="分类").grid(row=1, column=0, sticky="w")
        self.gen_category_box = ttk.Combobox(
            generator, textvariable=self.gen_category, width=14,
            values=self._learned_labels("categories"),
        )
        self.gen_category_box.grid(row=1, column=1, sticky="w")
        ttk.Label(generator, text="排序").grid(row=2, column=0, sticky="w")
        self.gen_sort_box = ttk.Combobox(
            generator, textvariable=self.gen_sort, width=14,
            values=self._learned_labels("sorts"),
        )
        self.gen_sort_box.grid(row=2, column=1, sticky="w")
        self.gen_hint = ttk.Label(
            generator, text="", foreground="#888", font=("", 8),
        )
        self.gen_hint.grid(row=3, column=0, columnspan=2, sticky="w")
        generator_buttons = ttk.Frame(generator)
        generator_buttons.grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Button(
            generator_buttons, text="生成网址", width=9,
            command=self.generate_list_url,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            generator_buttons, text="学习当前网址", width=12,
            command=self.learn_current_url,
        ).pack(side="left")
        self.sweep_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            generator, text="全城扫描（学到的所有分类）",
            variable=self.sweep_all,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.gen_max_categories = tk.StringVar(value="3")
        max_row = ttk.Frame(generator)
        max_row.grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(max_row, text="最多扫几个分类").pack(side="left")
        ttk.Entry(
            max_row, textvariable=self.gen_max_categories, width=5
        ).pack(side="left", padx=4)
        self._refresh_learned_hint()

        filters = ttk.LabelFrame(
            frame, text="筛选条件（默认值在「设置 → 自动找店条件」里改）",
            padding=8,
        )
        filters.grid(row=5, column=0, columnspan=5, sticky="ew", pady=8)

        self.discover_city = tk.StringVar(
            value=str(self.settings.get("discover_city_keyword", ""))
        )
        self.discover_years = tk.StringVar(
            value=str(self.settings.get("discover_min_years", 0))
        )
        self.discover_photos = tk.StringVar(
            value=str(self.settings.get("discover_min_photos", 0))
        )
        self.discover_limit = tk.StringVar(
            value=str(self.settings.get("discover_limit", 300))
        )

        for column, (label, var) in enumerate((
            ("城市关键词", self.discover_city),
            ("最少经营年限", self.discover_years),
            ("最少照片数", self.discover_photos),
            ("最多核验数", self.discover_limit),
        )):
            ttk.Label(filters, text=label + "：").grid(
                row=0, column=column * 2, sticky="w", padx=(0, 4)
            )
            ttk.Entry(filters, textvariable=var, width=8).grid(
                row=0, column=column * 2 + 1, sticky="w", padx=(0, 16)
            )

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=6, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.discover_button = ttk.Button(
            button_frame, text="开始寻找", command=self.start_discover
        )
        self.discover_button.pack(side="left", padx=(0, 8))
        self.discover_stop_button = ttk.Button(
            button_frame, text="停止", command=self.stop_discover
        )
        self.discover_stop_button.pack(side="left", padx=8)
        ttk.Button(
            button_frame, text="刷新结果", command=self.refresh_candidates
        ).pack(side="left", padx=8)
        ttk.Button(
            button_frame, text="导出 CSV", command=self.export_candidates
        ).pack(side="left", padx=8)
        ttk.Button(
            button_frame, text="采集选中的店铺",
            command=self.collect_selected_candidates,
        ).pack(side="left", padx=8)
        ttk.Button(
            button_frame, text="清空结果", command=self.clear_candidates_ui
        ).pack(side="left", padx=8)

        self.discover_status_label = ttk.Label(
            frame, text="就绪", foreground="#555"
        )
        self.discover_status_label.grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(0, 4)
        )

        table_frame = ttk.Frame(frame)
        table_frame.grid(row=8, column=0, columnspan=4, sticky="nsew")
        columns = ("name", "city", "photos", "oldest", "years", "status", "id")
        headings = {
            "name": ("店铺名称", 260), "city": ("城市", 90),
            "photos": ("照片数", 80), "oldest": ("最早照片", 110),
            "years": ("至少经营", 90), "status": ("状态", 90),
            "id": ("店铺 ID", 140),
        }
        self.candidate_tree = ttk.Treeview(
            table_frame, columns=columns, show="headings",
            selectmode="extended",
        )
        for key in columns:
            title, width = headings[key]
            self.candidate_tree.heading(key, text=title)
            self.candidate_tree.column(key, width=width, anchor="w")
        tree_scroll = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.candidate_tree.yview
        )
        self.candidate_tree.configure(yscrollcommand=tree_scroll.set)
        self.candidate_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=1)
        frame.columnconfigure(3, weight=1)
        frame.rowconfigure(8, weight=1)

        self.refresh_candidates()

    def _learned_labels(self, kind):
        return [
            item.get("label", "")
            for item in (self.learned_filters.get(kind) or [])
            if item.get("label")
        ]

    def _refresh_learned_hint(self):
        counts = [
            f"{title} {len(self.learned_filters.get(kind) or [])}"
            for kind, title in (
                ("categories", "分类"), ("sorts", "排序"),
                ("groups", "二级分类"), ("regions", "商圈"),
            )
            if self.learned_filters.get(kind)
        ]
        try:
            self.gen_hint.config(
                text="已学到的对应关系：" + "、".join(counts)
                if counts else "还没学过：填好网址后点「学习当前网址」"
            )
        except Exception:
            pass

    def generate_list_url(self):
        """按「城市 + 分类 + 排序」拼出列表页地址，填进上面的网址框。"""
        city = self.gen_city.get().strip()
        category = self.gen_category.get().strip()
        sort = self.gen_sort.get().strip()
        pinyin = city_pinyin(city)
        if not pinyin:
            messagebox.showwarning(
                "认不出城市",
                f"「{city}」不在内置城市表里。\n"
                "可以直接填点评地址里的拼音（例如 chaozhou），"
                "或者用浏览器打开该城市的列表页另存为 HTML 后点「学习当前网址」。",
            )
            return
        # 先在学到的对应关系里找（页面上学到的真实链接最准），找不到再按语法拼
        picked_category = find_learned_option(
            self.learned_filters, "categories", category
        )
        picked_sort = find_learned_option(self.learned_filters, "sorts", sort)
        if picked_sort and picked_sort.get("url"):
            url = picked_sort["url"].replace(
                f"https://www.dianping.com/{parse_list_url(picked_sort['url'])['city']}",
                f"https://www.dianping.com/{pinyin}",
                1,
            )
        else:
            url = build_list_url(
                pinyin,
                (picked_category or {}).get("code", category) or "ch10",
                sort=(picked_sort or {}).get("code", sort),
            )
        self.discover_url.set(url)
        self.log(f"[GEN] 生成网址：{url}")
        self._refresh_learned_hint()

    def learn_current_url(self):
        """抓当前网址（或本地 HTML）并学习分类/排序对应关系。"""
        url = self.discover_url.get().strip()
        if not url:
            messagebox.showwarning("缺少网址", "请先填写列表页网址。")
            return
        self.settings.set("list_url", url)
        self.settings.save()
        self.http.apply_login_state(self.settings)
        discovery = ShopDiscovery(
            self.http, self.settings, db=None, logger=self.log,
        )
        learned, error = discovery.learn_filters(url=url)
        if error:
            self.log(f"[GEN] 学习失败：{error}")
            messagebox.showwarning("学习失败", error)
            return
        self.learned_filters = learned
        self.settings.set("learned_filters", learned)
        self.settings.save()
        try:
            self.gen_category_box.configure(values=self._learned_labels("categories"))
            self.gen_sort_box.configure(values=self._learned_labels("sorts"))
        except Exception:
            pass
        self._refresh_learned_hint()
        summary = "\n".join(
            f"{title}：{len(learned.get(kind) or [])} 个"
            for kind, title in (
                ("categories", "一级分类"), ("sorts", "排序方式"),
                ("groups", "二级分类"), ("regions", "商圈"),
                ("cities", "城市"),
            )
        )
        self.log(f"[GEN] 学习完成：{summary.replace(chr(10), '；')}")
        for kind, title in (("categories", "一级分类"), ("sorts", "排序方式")):
            for item in (learned.get(kind) or [])[:12]:
                self.log(
                    f"[GEN]   {title} {item.get('label')} = {item.get('code')}"
                    f"  {item.get('url')}"
                )
        messagebox.showinfo(
            "学习完成",
            summary + "\n\n现在可以在下拉框里选分类/排序，"
            "或改城市后点「生成网址」。",
        )

    def _refresh_cookie_status(self):
        cookie = str(self.settings.get("cookie", "") or "")
        if cookie:
            names = [
                part.split("=")[0].strip()
                for part in cookie.split(";") if "=" in part
            ]
            self.cookie_status_label.config(
                text=f"已配置登录态（{len(names)} 个 Cookie）",
                foreground="#008800",
            )
        else:
            self.cookie_status_label.config(
                text="未配置登录态（列表页会被风控拦住）", foreground="#aa0000"
            )

    def parse_cookie_input(self):
        """把「Copy as cURL」粘贴内容解析成 Cookie / UA / 网址。"""
        raw = self.discover_cookie_text.get("1.0", "end").strip()
        if not raw:
            messagebox.showwarning("没有内容", "请先粘贴 cURL 命令或 Cookie。")
            return
        parsed = parse_curl_command(raw)
        cookie = parsed.get("cookie") or ""
        user_agent = parsed.get("user_agent") or ""
        url = parsed.get("url") or ""
        if not cookie and "=" in raw and "curl" not in raw[:30].lower():
            cookie = " ".join(raw.split())
        if not cookie:
            messagebox.showwarning(
                "没识别到 Cookie",
                "这看起来不是 cURL 命令，也没找到 key=value 形式的 Cookie。\n\n"
                "正确做法：在已登录的浏览器里打开列表页 → 按 F12 → Network →"
                "刷新 → 点第一个请求 → 右键 →「复制」→「以 cURL 格式复制」→ "
                "粘贴到上面的输入框。",
            )
            return

        self.settings.set("cookie", cookie)
        if user_agent:
            self.settings.set("user_agent", user_agent)
        if url and not self.discover_url.get().strip():
            self.discover_url.set(url)
            self.settings.set("list_url", url)
        self.settings.save()
        self.http.apply_login_state(self.settings)
        self._refresh_cookie_status()
        self.log(f"[LOGIN] 已保存登录态（{len(cookie)} 字符）")
        messagebox.showinfo(
            "已保存",
            f"已保存登录态：{len(cookie)} 字符\n"
            + (f"已填入网址：{url}\n" if url else "")
            + "点「测试列表页」可以立刻验证能不能访问。",
        )

    def clear_cookie_input(self):
        """清除本机保存的登录态（Cookie 明文保存在设置文件里，随时可以清掉）。"""
        self.settings.set("cookie", "")
        self.settings.set("user_agent", "")
        self.settings.save()
        self.http.apply_login_state(self.settings)
        try:
            self.discover_cookie_text.delete("1.0", "end")
        except Exception:
            pass
        self._refresh_cookie_status()
        self.log("[LOGIN] 已清除本机保存的登录态")

    def test_list_url(self):
        url = self.discover_url.get().strip()
        if url:
            self.settings.set("list_url", url)
        self.settings.save()
        self.http.apply_login_state(self.settings)
        discovery = ShopDiscovery(
            self.http, self.settings, db=None, logger=self.log,
        )
        ok, message = discovery.test_list_url(url)
        self.log(f"[LOGIN] 列表页测试：{'可访问' if ok else '不可访问'} - {message}")
        if ok:
            messagebox.showinfo("列表页可访问", message)
        else:
            messagebox.showwarning("列表页不可访问", message)

    def choose_discover_files(self):
        paths = filedialog.askopenfilenames(
            title="选择浏览器另存为的列表页 / 搜索结果页",
            filetypes=[
                ("网页文件", "*.html *.htm *.txt"),
                ("所有文件", "*.*"),
            ],
        )
        if not paths:
            return
        self.discover_paths = list(paths)
        names = "、".join(Path(p).name for p in self.discover_paths[:3])
        if len(self.discover_paths) > 3:
            names += f" 等 {len(self.discover_paths)} 个文件"
        self.discover_path_label.config(text=names, foreground="#000")

    def _discover_filters(self):
        def as_int(var, default=0):
            try:
                return int(str(var.get()).strip() or default)
            except ValueError:
                return default

        return {
            "city_keyword": self.discover_city.get().strip(),
            "min_years": as_int(self.discover_years, 0),
            "min_photos": as_int(self.discover_photos, 0),
            "limit": as_int(self.discover_limit, 300) or 300,
            "delay": float(self.settings.get("discover_delay", 1.2) or 1.2),
        }

    def start_discover(self):
        if self.discover_thread and self.discover_thread.is_alive():
            messagebox.showinfo("正在运行", "上一次查找还没结束，请先等待或点「停止」。")
            return

        source = self.discover_source.get()
        pasted = self.discover_text.get("1.0", "end").strip()
        list_url = self.discover_url.get().strip()
        max_pages = 3

        if source == "url":
            if not list_url:
                messagebox.showwarning(
                    "缺少网址",
                    "请填写列表页 / 搜索结果页网址，例如\n"
                    "https://www.dianping.com/shanghai/ch10",
                )
                return
            if not str(self.settings.get("cookie", "") or ""):
                if not messagebox.askyesno(
                    "还没有登录态",
                    "没有 Cookie 时，点评的列表页几乎一定会跳到验证中心。\n\n"
                    "建议先在已登录的浏览器里「Copy as cURL」并点「解析 cURL」。\n\n"
                    "仍然要继续尝试吗？",
                ):
                    return
            try:
                max_pages = max(
                    1, int(self.discover_max_pages.get().strip() or 1)
                )
            except ValueError:
                max_pages = 1
            self.settings.set("list_url", list_url)
            self.settings.set("list_max_pages", max_pages)
        elif source == "files":
            if not self.discover_paths:
                messagebox.showwarning(
                    "缺少来源",
                    "请先选择浏览器另存为的 HTML 文件。\n\n"
                    "方法：浏览器打开点评的列表页或搜索结果页 → "
                    "Ctrl+S 另存为「网页，仅 HTML」→ 在这里选中它。",
                )
                return
        elif source == "text":
            if not pasted:
                messagebox.showwarning("缺少来源", "请先把含点评链接的文本粘贴进来。")
                return

        filters = self._discover_filters()
        # 把筛选条件回写到设置里，下次打开就是上次的值
        self.settings.set("discover_city_keyword", filters["city_keyword"])
        self.settings.set("discover_min_years", filters["min_years"])
        self.settings.set("discover_min_photos", filters["min_photos"])
        self.settings.set("discover_limit", filters["limit"])
        self.settings.save()
        self.http.apply_login_state(self.settings)

        self.discover_stop.clear()
        self.discover_pause.clear()
        self.discovery = ShopDiscovery(
            http_client=self.http, settings=self.settings, db=self.db,
            logger=self.log,
            pause_event=self.discover_pause, stop_event=self.discover_stop,
            progress=self._on_discover_progress,
        )
        self.discover_status_label.config(text="正在寻找…", foreground="#0055aa")
        self.discover_button.configure(state="disabled")
        self.discover_thread = threading.Thread(
            target=self._discover_worker,
            args=(source, {"pasted": pasted, "list_url": list_url,
                           "max_pages": max_pages,
                           "sweep": bool(self.sweep_all.get()),
                           "city": self.gen_city.get().strip(),
                           "category": self.gen_category.get().strip(),
                           "sort": self.gen_sort.get().strip(),
                           "max_categories": self.gen_max_categories.get().strip()},
                  filters),
            daemon=True,
        )
        self.discover_thread.start()
        self.log("[DISCOVER] 已开始自动找店")

    def _discover_worker(self, source, payload, filters):
        try:
            if source == "url" and payload.get("sweep"):
                try:
                    max_categories = int(payload.get("max_categories") or 0)
                except ValueError:
                    max_categories = 0
                results, stats = self.discovery.discover_city(
                    city=payload.get("city") or "上海",
                    category=payload.get("category") or "",
                    sort=payload.get("sort") or "",
                    max_pages=payload.get("max_pages", 3),
                    max_categories=max_categories,
                    source="城市扫描", **filters
                )
            elif source == "url":
                results, stats = self.discovery.discover_from_url(
                    payload["list_url"], max_pages=payload["max_pages"],
                    source="列表页", **filters,
                )
            elif source == "files":
                results, stats = self.discovery.discover_from_files(
                    self.discover_paths, source="本地HTML", **filters
                )
            elif source == "text":
                results, stats = self.discovery.discover_from_text(
                    payload["pasted"], source="粘贴文本", **filters
                )
            else:
                results, stats = self.discovery.discover_from_archive(**filters)

            if stats.get("error"):
                self.log(f"[DISCOVER] 中止：{stats['error']}")
            self.log(
                f"[DISCOVER] 核验 {stats['checked']}/{stats['total']} 家，"
                f"符合条件 {stats['passed']} 家，跳过 {stats['failed']} 家"
            )
            for reason, count in sorted(
                stats.get("reasons", {}).items(), key=lambda x: -x[1]
            )[:6]:
                self.log(f"[DISCOVER]   跳过原因 {count} 家：{reason}")
            if stats["passed"] == 0 and stats["checked"]:
                self.log(
                    "[DISCOVER] 提示：如果全是「被点评风控拦截」，"
                    "请检查登录态；如果全是「未找到商户信息」，"
                    "说明抓到的不是相册页/不是店铺链接。"
                )
            self.discover_queue.put(("done", stats, None))
        except Exception as exc:
            self.log(f"[DISCOVER] 失败：{type(exc).__name__}: {exc}")
            self.discover_queue.put(("done", None, None))

    def _on_discover_progress(self, done, total, info):
        self.discover_queue.put(("progress", (done, total), info))

    def _drain_discover_queue(self):
        try:
            while True:
                kind, payload, info = self.discover_queue.get_nowait()
                if kind == "progress":
                    done, total = payload
                    self.discover_status_label.config(
                        text=f"已核验 {done}/{total}…", foreground="#0055aa"
                    )
                elif kind == "done":
                    self.discover_button.configure(state="normal")
                    if payload:
                        self.discover_status_label.config(
                            text=(
                                f"完成：核验 {payload['checked']} 家，"
                                f"符合条件 {payload['passed']} 家"
                            ),
                            foreground="#008800",
                        )
                    else:
                        self.discover_status_label.config(
                            text="已结束或失败，详见日志", foreground="#aa0000"
                        )
                    self.refresh_candidates()
        except queue.Empty:
            pass

    def stop_discover(self):
        if self.discover_thread and self.discover_thread.is_alive():
            self.discover_stop.set()
            self.discover_status_label.config(text="正在停止…", foreground="#aa0000")
            self.log("[DISCOVER] 已请求停止")
        else:
            self.log("[DISCOVER] 当前没有在运行")

    def refresh_candidates(self):
        # 界面是在数据库打开之前建好的，启动时会被调用一次；
        # 这时还读不到候选，静默返回即可（以前会打一行 'NoneType' 报错）。
        if getattr(self, "candidate_tree", None) is None:
            return 0
        for item in self.candidate_tree.get_children():
            self.candidate_tree.delete(item)
        if getattr(self, "db", None) is None:
            return 0
        try:
            rows = self.db.get_candidates()
        except Exception as exc:
            self.log(f"[DISCOVER] 读取候选失败：{exc}")
            return 0
        for (shop_id, name, city, photo_count, oldest, years, source,
             status, note) in rows:
            label = {
                "candidate": "候选", "collected": "已采集",
                "ignored": "已忽略",
            }.get(status, status)
            self.candidate_tree.insert(
                "", "end", iid=shop_id,
                values=(
                    name or "(未知)", city or "", photo_count or "",
                    oldest or "", f"{years:.0f} 年" if years else "",
                    label, shop_id,
                ),
            )
        return len(rows)

    def export_candidates(self):
        if getattr(self, "db", None) is None:
            messagebox.showinfo("请稍候", "数据库还没打开，稍后再试。")
            return
        rows = self.db.get_candidates()
        if not rows:
            messagebox.showinfo("没有结果", "候选列表是空的。")
            return
        path = filedialog.asksaveasfilename(
            title="导出候选店铺",
            defaultextension=".csv",
            initialfile="dianping_candidates.csv",
            filetypes=[("CSV 文件", "*.csv")],
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    "shop_id", "shop_name", "city", "photo_count",
                    "oldest_photo_at", "estimated_years", "source",
                    "status", "note", "url",
                ])
                for (shop_id, name, city, photo_count, oldest, years, source,
                     status, note) in rows:
                    writer.writerow([
                        shop_id, name, city, photo_count, oldest, years,
                        source, status, note,
                        page_url(shop_id, 1),
                    ])
            self.log(f"[DISCOVER] 已导出 {len(rows)} 条候选到 {path}")
            messagebox.showinfo("导出完成", f"已导出 {len(rows)} 条候选。")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def collect_selected_candidates(self):
        if self.discover_thread and self.discover_thread.is_alive():
            messagebox.showinfo("正在查找", "请先停止查找，再开始采集。")
            return
        selected = list(self.candidate_tree.selection())
        if not selected:
            messagebox.showinfo("未选择", "请先在表格里选择要采集的店铺。")
            return
        answer = messagebox.askyesno(
            "开始采集",
            f"将采集选中的 {len(selected)} 家店铺（完整相册，新→旧）。\n"
            f"「设置 → 采集日期范围」里的条件会同时生效。\n\n确定继续吗？",
        )
        if not answer:
            return
        if self.controller and self.controller.running:
            messagebox.showinfo("正在采集", "已有采集任务在运行。")
            return

        base = Path(self.base_dir.get())
        base.mkdir(parents=True, exist_ok=True)
        self.progress.start(10)
        self.controller.start(
            input_url="", output_dir=str(base), mode="auto_all",
            extra_shop_ids=selected,
        )
        self.log(f"[CTRL] 已排队采集 {len(selected)} 家候选店铺")

    def clear_candidates_ui(self):
        if not messagebox.askyesno(
            "清空结果", "清空候选列表？（已采集的档案不受影响）"
        ):
            return
        removed = self.db.clear_candidates()
        self.refresh_candidates()
        self.log(f"[DISCOVER] 已清空 {removed} 条候选")

    def create_browser(self):
        db_path = str(Path(self.base_dir.get()) / "archive.db")
        Path(self.base_dir.get()).mkdir(parents=True, exist_ok=True)
        self.browser = ArchiveBrowser(
            self.browser_tab, db_path,
            self.base_dir.get(), self.settings,
        )

    def ensure_database(self):
        base = Path(self.base_dir.get())
        base.mkdir(parents=True, exist_ok=True)
        db_path = base / "archive.db"
        self.db = ArchiveDB(str(db_path))
        self.controller = CrawlController(
            db=self.db, http_client=self.http,
            resolver=self.resolver, settings=self.settings,
            logger=self.log,
        )

    def choose_dir(self):
        directory = filedialog.askdirectory(title="选择档案保存目录")
        if not directory:
            return
        self.base_dir.set(directory)
        try:
            if self.db:
                self.db.close()
        except Exception:
            pass
        self.ensure_database()
        try:
            self.browser.destroy()
        except Exception:
            pass
        for child in self.browser_tab.winfo_children():
            child.destroy()
        self.browser = ArchiveBrowser(
            self.browser_tab,
            str(Path(directory) / "archive.db"),
            directory, self.settings,
        )
        self.log(f"[DIR] 保存目录：{directory}")
        try:
            self.refresh_candidates()
        except Exception:
            pass

    def update_range_state(self):
        mode = self.mode_var.get()
        if mode == "auto_all":
            self.start_entry.configure(state="disabled")
            self.stop_entry.configure(state="disabled")
            self.photo_count_entry.configure(state="disabled")
        elif mode == "auto_to":
            self.start_entry.configure(state="disabled")
            self.stop_entry.configure(state="normal")
            self.photo_count_entry.configure(state="disabled")
        elif mode == "count_all":
            self.start_entry.configure(state="disabled")
            self.stop_entry.configure(state="disabled")
            self.photo_count_entry.configure(state="normal")
        elif mode == "count_to":
            self.start_entry.configure(state="disabled")
            self.stop_entry.configure(state="normal")
            self.photo_count_entry.configure(state="normal")
        else:
            self.start_entry.configure(state="normal")
            self.stop_entry.configure(state="normal")
            self.photo_count_entry.configure(state="disabled")

    def start_crawl(self):
        raw = self.url_var.get().strip()
        if not raw:
            messagebox.showwarning(
                "缺少 URL",
                "请输入大众点评公开商户链接或整段分享文本。",
            )
            return

        url = extract_dianping_url(raw)
        if not url:
            messagebox.showwarning(
                "无法识别链接",
                "输入中未找到有效的大众点评链接。",
            )
            return

        mode = self.mode_var.get()
        start_page = None
        stop_page = None
        photo_count = None

        if mode == "auto_to":
            try:
                stop_page = int(self.stop_var.get())
                if stop_page < 1:
                    raise ValueError
            except Exception:
                messagebox.showerror(
                    "页码错误", "终止页必须是大于 0 的整数。"
                )
                return
        elif mode in ("count_all", "count_to"):
            try:
                photo_count = int(self.photo_count_var.get())
                if photo_count < 1:
                    raise ValueError
            except Exception:
                messagebox.showerror(
                    "图片数量错误", "图片数量必须是大于 0 的整数。"
                )
                return
            if mode == "count_to":
                try:
                    stop_page = int(self.stop_var.get())
                    if stop_page < 1:
                        raise ValueError
                except Exception:
                    messagebox.showerror(
                        "页码错误", "终止页必须是大于 0 的整数。"
                    )
                    return
        elif mode == "custom":
            try:
                start_page = int(self.start_var.get())
                stop_page = int(self.stop_var.get())
                if start_page < 1 or stop_page < 1:
                    raise ValueError
            except Exception:
                messagebox.showerror(
                    "页码错误", "页码必须是大于 0 的整数。"
                )
                return

        base = Path(self.base_dir.get())
        base.mkdir(parents=True, exist_ok=True)
        self.progress.start(10)

        date_from = normalize_date_bound(self.settings.get("date_from", ""))
        date_to = normalize_date_bound(self.settings.get("date_to", ""))
        if date_from or date_to:
            self.log(
                f"[DATE] 只采集 {date_from or '不限'} ~ {date_to or '不限'}"
                f" 的照片（在「设置 → 采集日期范围」里修改）"
            )

        self.controller.start(
            input_url=url, output_dir=str(base), mode=mode,
            start_page=start_page, stop_page=stop_page,
            photo_count=photo_count,
        )
        self.log("[CTRL] 已启动任务")

    def pause_crawl(self):
        if self.controller:
            self.controller.pause()

    def resume_crawl(self):
        if self.controller:
            self.controller.resume()

    def stop_crawl(self):
        if self.controller:
            self.controller.stop()
        self.progress.stop()

    def cleanup_duplicates(self):
        if not self.db:
            return

        answer = messagebox.askyesno(
            "清理重复文件",
            "将删除数据库中标记为「重复」的本地文件。\n"
            "数据库记录会保留，但文件被物理删除。\n\n"
            "确定继续吗？",
        )
        if not answer:
            return

        try:
            with self.db.lock:
                rows = self.db.conn.execute(
                    """
                    SELECT id, local_path, status
                    FROM images
                    WHERE duplicate_of IS NOT NULL
                      AND local_path IS NOT NULL
                      AND local_path != ''
                    """
                ).fetchall()

            deleted = 0
            failed = 0
            freed = 0

            for image_id, path_str, status in rows:
                p = Path(path_str)
                if not p.exists():
                    continue
                try:
                    size = p.stat().st_size
                    p.unlink()
                    freed += size
                    deleted += 1
                except Exception:
                    failed += 1

            self.log(
                f"[CLEANUP] 删除 {deleted} 个重复文件，"
                f"释放 {freed / 1024 / 1024:.1f} MB，"
                f"失败 {failed} 个"
            )
            messagebox.showinfo(
                "清理完成",
                f"删除 {deleted} 个文件\n"
                f"释放 {freed / 1024 / 1024:.1f} MB\n"
                f"失败 {failed} 个",
            )
        except Exception as e:
            self.log(f"[CLEANUP] 失败：{e}")

    def log(self, message):
        timestamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")

    def flush_logs(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.insert("end", message + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        self._drain_discover_queue()
        self.root.after(100, self.flush_logs)

    def refresh_browser(self):
        try:
            self.browser.refresh_shops()
            self.refresh_photo_count_hint()
            self.log("[BROWSER] 档案库已刷新")
        except Exception as e:
            self.log(f"[BROWSER] 刷新失败：{e}")


# ============================================================
# main
# ============================================================

# ============================================================
# 命令行自检（不需要 GUI，也不需要联网）
# ============================================================

USAGE = f"""
{APP_NAME}  v{APP_VERSION}

用法：
  python {Path(__file__).name}                     启动图形界面
  python {Path(__file__).name} --parse-html <文件>  用真实 page.html 自检解析结果
  python {Path(__file__).name} --selftest          内置样例自检（推荐先跑这个）
  python {Path(__file__).name} --check-deps        检查依赖是否齐全
  python {Path(__file__).name} --probe <店铺ID>     查询一家店的名称/城市/照片数/最早照片
  python {Path(__file__).name} --discover <文件…>   从浏览器另存为的列表页里找店
        [--city 上海] [--min-years 15] [--min-photos 300] [--limit 200]
  python {Path(__file__).name} --discover-url <网址> 带登录态自动翻页找店（推荐）
        [--cookie "<Cookie>" | --curl <CopyAsCurl.txt>] [--max-pages 3]
        [--city 上海] [--min-years 15] [--min-photos 300] [--limit 200]
  python {Path(__file__).name} --discover-city <城市> 按城市大范围扫描（自动学分类）
        [--category ch10|美食] [--sort o11|评价最多] [--max-pages 2]
        [--max-categories 3] [--curl CopyAsCurl.txt] [--min-years 15]

列表页地址语法（可以自己拼，也可以从页面里学）：
  /{{城市拼音}}/ch{{一级分类}}[/{{筛选段}}]        筛选段：g{{二级分类}} r{{商圈}} o{{排序}}
  例：/shanghai/ch10 = 上海·餐饮      /shanghai/ch10/o11 = 上海·餐饮·评价最多
  多个筛选段要拼在一起：/shanghai/ch10/g116o2 = 西餐·人气
  （实测 /shanghai/ch10/g116/o2 会 404，别用斜杠把筛选段分开）

翻页写法（V3.4 修正，务必注意）：
  只有一级分类时      /shanghai/ch10/p2        可以
  带排序/二级/商圈时  /shanghai/ch10/o11p2     可以（页码紧贴上一段）
  同样的地址写斜杠式  /shanghai/ch10/o11/p2    会被 403 ← 你之前遇到的翻页失败
  ?pg=2              返回 200 但内容仍是第 1 页（会被忽略，不要用）
  程序现在优先跟页面自带的「下一页」链接，所以不用自己操心页码写法。
  python {Path(__file__).name} --list-url 上海 --category ch10 --sort o11
  python {Path(__file__).name} --learn-url https://www.dianping.com/shanghai/ch10 --curl CopyAsCurl.txt
  python {Path(__file__).name} --learn-html <浏览器另存为的.html>   （不需要登录态）

登录态怎么拿：已登录的浏览器打开列表页 → F12 → Network → 刷新 → 点第一个请求 →
右键 →「复制」→「以 cURL 格式复制」→ 存成 txt 用 --curl 传入（界面里直接粘贴即可）。

采集后如果怀疑作者/时间不对，可以这样核对：
  python {Path(__file__).name} --parse-html <保存目录>/<shop_id>/page_1/page.html
"""


# 内置样例：完全按 2026-09 实测的大众点评相册卡片结构构造
SELFTEST_HTML = """<!doctype html><html><body>
<div class="gallery-list-wrapper page-block">
  <ul class="picture-list">
    <li class="J_list">
      <div class="img">
        <span class="hook"></span>
        <a class="J_entry" href="/photos/7605587866" data-index="0" target="_blank"
           onclick="pageTracker._trackPageview('dp_shop_photo_pic_shanghai_');">
          <img src="https://qcloud.dpfile.com/pc/l6bmVGGT4L7K-VkhA8G59MUXlC2P9d4gQ29BPNnFgm3cRzeyFcO-q9cZdhmj6bv5l0cm-Lf9tDMlLZpO7rb3bg.jpg"
               title="点击看大图" alt="-测试店铺(测试路店)"></a>
      </div>
      <div class="picture-info">
        <div class="name"><h3>
          <a class="J_entry" href="/photos/7605587866" data-index="0" target="_blank" title=""></a>
        </h3></div>
        <div class="info">
          <a rel="nofollow" href="/member/1978375941" target="_blank" title="流东向水春江一"
             onclick="pageTracker._trackPageview('dp_shop_photo_name_shanghai_');">流东向水春江一</a>
          <em class="sep">|</em>
          <span>25-12-18</span>
          <div class="digg-box">
            <a class="Hide J_report report" rel="nofollow" title="报错" href="javascript:"
               onclick="pageTracker._trackPageview('dp_report_piclist_shanghai_');return $PicReport(7605587866, 1978375941, '流东向水春江一');">报错</a>
          </div>
        </div>
      </div>
    </li>
    <li class="J_list">
      <div class="img">
        <span class="hook"></span>
        <a class="J_entry" href="/photos/8422131633" data-index="1" target="_blank">
          <img src="https://img.meituan.net/ugcpic/6c08c3006402501fbf08423329cb597f558858.jpg%40240w_180h_1e_1c_1l%7Cwatermark%3D0"
               title="点击看大图" alt="-测试店铺(测试路店)"></a>
      </div>
      <div class="picture-info">
        <div class="name"><h3>
          <a class="J_entry" href="/photos/8422131633" data-index="1" target="_blank" title=""></a>
        </h3></div>
        <div class="info">
          <a rel="nofollow" href="/member/4553449812" target="_blank" title="春暖花开">春暖花开</a>
          <em class="sep">|</em>
          <span>01-15</span>
          <div class="digg-box">
            <a class="Hide J_report report" rel="nofollow" title="报错" href="javascript:"
               onclick="return $PicReport(8422131633, 4553449812, '春暖花开');">报错</a>
          </div>
        </div>
      </div>
    </li>
    <li class="J_list">
      <div class="img">
        <span class="hook"></span>
        <a class="J_entry" href="/photos/8869312192" data-index="2" target="_blank">
          <img src="https://qcloud.dpfile.com/pc/tIG071kxZPRXDnewKF0YbAm_31nvkak5JjtN3oqCmOOMSIggNRFlDuhsaRPlsZbml0cm-Lf9tDMlLZpO7rb3bg.jpg"
               title="点击看大图" alt="-测试店铺(测试路店)"></a>
      </div>
      <div class="picture-info">
        <div class="name"><h3>
          <a class="J_entry" href="/photos/8869312192" data-index="2" target="_blank" title=""></a>
        </h3></div>
        <div class="info">
          <a rel="nofollow" title="" onclick="pageTracker._trackPageview('x');"></a>
          <em class="sep">|</em>
          <span>24-03-17</span>
          <div class="digg-box">
            <a class="Hide J_report report" rel="nofollow" title="报错" href="javascript:"
               onclick="return $PicReport(8869312192, -1, '');">报错</a>
          </div>
        </div>
      </div>
    </li>
  </ul>
</div>
</body></html>
"""


def read_text_file(path):
    """按常见编码尝试读取本地文本 / HTML 文件。"""
    for encoding in ("utf-8", "utf-8-sig", "gbk", "utf-16", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError:
            return None
    return None


def cli_check_deps():
    print(f"{APP_NAME} v{APP_VERSION}")
    print(f"Python        : {sys.version.split()[0]}")
    print(f"beautifulsoup4: {'OK' if BS4_AVAILABLE else '缺失 -> ' + BS4_ERROR}")
    for label, module_name in (
        ("requests", "requests"),
        ("Pillow", "PIL"),
        ("imagehash", "imagehash"),
        ("tkinter", "tkinter"),
    ):
        try:
            __import__(module_name)
            print(f"{label:<14}: OK")
        except Exception as exc:
            print(f"{label:<14}: 缺失 -> {exc}")
    if not BS4_AVAILABLE:
        print("\n请先执行：pip install beautifulsoup4")
        return 1
    return 0


def cli_parse_html(path):
    if not path:
        print("用法：--parse-html <page.html 路径>")
        return 2
    if not Path(path).exists():
        print(f"文件不存在：{path}")
        return 2

    html = read_text_file(path)
    if not html:
        print(f"无法读取文件：{path}")
        return 2

    parser = DianpingHTMLParser()
    image_urls = parser.extract_image_urls(html)
    metadata = parser.extract_image_metadata(html, image_urls)
    diagnostics = parser.last_diagnostics or {}

    print(f"文件       : {path}")
    print(f"提取图片   : {len(image_urls)}")
    print(
        f"相册卡片   : {diagnostics.get('cards', 0)}"
        f"（含作者 {diagnostics.get('cards_with_uploader', 0)}，"
        f"含时间 {diagnostics.get('cards_with_date', 0)}）"
    )
    if diagnostics.get("error"):
        print(f"解析错误   : {diagnostics['error']}")
    print("-" * 100)
    print(
        f"{'#':>3} {'图片ID':<12} {'作者':<18} {'发布时间':<20} "
        f"{'原始':<10} {'置信':>4}  图片"
    )
    print("-" * 100)
    for index, url in enumerate(image_urls, start=1):
        item = metadata.get(canonical_image_url(url), {}) or {}
        print(
            f"{index:>3} {str(item.get('photo_id', '')):<12} "
            f"{str(item.get('uploader', ''))[:18]:<18} "
            f"{str(item.get('published_at', '')):<20} "
            f"{str(item.get('published_raw', '')):<10} "
            f"{int(item.get('confidence', 0) or 0):>4}  "
            f"{url[:70]}"
        )
    print("-" * 100)
    if not BS4_AVAILABLE:
        print("提示：未安装 beautifulsoup4，本次解析不会得到作者/时间。")
    return 0


def cli_selftest():
    print(f"{APP_NAME} v{APP_VERSION} 内置自检")
    if not BS4_AVAILABLE:
        print(f"{console_symbol(False)} beautifulsoup4 缺失：{BS4_ERROR}")
        print("  请执行：pip install beautifulsoup4")
        return 1

    now = datetime.now()
    parser = DianpingHTMLParser()
    image_urls = parser.extract_image_urls(SELFTEST_HTML)

    # 故意混入「不同尺寸后缀」的地址，验证 basename 兜底绑定
    requested = list(image_urls) + [
        "https://img.meituan.net/ugcpic/"
        "6c08c3006402501fbf08423329cb597f558858.jpg@600w_600h_1e_1c.webp"
    ]
    metadata = parser.extract_image_metadata(SELFTEST_HTML, requested)

    def get(url):
        return metadata.get(canonical_image_url(url), {}) or {}

    card1 = get("https://qcloud.dpfile.com/pc/"
                "l6bmVGGT4L7K-VkhA8G59MUXlC2P9d4gQ29BPNnFgm3cRzeyFcO"
                "-q9cZdhmj6bv5l0cm-Lf9tDMlLZpO7rb3bg.jpg")
    card2 = get("https://img.meituan.net/ugcpic/"
                "6c08c3006402501fbf08423329cb597f558858.jpg"
                "%40240w_180h_1e_1c_1l%7Cwatermark%3D0")
    card3 = get("https://qcloud.dpfile.com/pc/"
                "tIG071kxZPRXDnewKF0YbAm_31nvkak5JjtN3oqCmOOMSIggNRFlDuhsaRPlsZbml"
                "0cm-Lf9tDMlLZpO7rb3bg.jpg")
    card2_variant = get("https://img.meituan.net/ugcpic/"
                        "6c08c3006402501fbf08423329cb597f558858.jpg"
                        "@600w_600h_1e_1c.webp")

    failures = []

    def check(condition, label, detail=""):
        safe_print(
            f"{console_symbol(condition)} {label}" + (f"  {detail}" if detail else "")
        )
        if not condition:
            failures.append(label)

    check(card1.get("uploader") == "流东向水春江一",
          "卡片1 作者 = 流东向水春江一", f"实际 {card1.get('uploader')!r}")
    check(card1.get("published_at") == "2025-12-18",
          "卡片1 时间 = 2025-12-18（YY-MM-DD 跨年）",
          f"实际 {card1.get('published_at')!r}")
    check(card1.get("published_raw") == "25-12-18",
          "卡片1 原始文本 = 25-12-18", f"实际 {card1.get('published_raw')!r}")
    check(card1.get("photo_id") == "7605587866",
          "卡片1 图片ID = 7605587866", f"实际 {card1.get('photo_id')!r}")
    check(card1.get("year_inferred") is False,
          "卡片1 年份不是推算出来的")

    check(card2.get("uploader") == "春暖花开",
          "卡片2 作者 = 春暖花开", f"实际 {card2.get('uploader')!r}")
    check(str(card2.get("published_at", "")).endswith("-01-15"),
          "卡片2 时间补全为 YYYY-01-15", f"实际 {card2.get('published_at')!r}")
    check(
        str(card2.get("published_at", ""))[:4] in (
            str(now.year), str(now.year - 1)
        ),
        "卡片2 年份来自 MM-DD 推算", f"实际 {card2.get('published_at')!r}",
    )
    check(card2.get("year_inferred") is True, "卡片2 标记 year_inferred=True")
    check(card2.get("published_raw") == "01-15",
          "卡片2 原始文本 = 01-15", f"实际 {card2.get('published_raw')!r}")
    check(card2_variant.get("uploader") == "春暖花开",
          "同一张图换尺寸后缀仍能绑定作者",
          f"实际 {card2_variant.get('uploader')!r}")

    check(card3.get("uploader") == "",
          "卡片3 匿名图片作者留空（不猜）", f"实际 {card3.get('uploader')!r}")
    check(card3.get("published_at") == "2024-03-17",
          "卡片3 时间 = 2024-03-17", f"实际 {card3.get('published_at')!r}")

    counts = sum(
        1 for item in metadata.values()
        if item.get("uploader") or item.get("published_at")
    )
    check(counts == len(requested),
          "所有请求的图片都拿到元数据", f"{counts}/{len(requested)}")

    print("-" * 60)
    if failures:
        print(f"自检失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"自检全部通过 {console_symbol(True)}")
    return 0


def cli_probe(shop_ids):
    """查询一家或多家店铺：名称 / 城市 / 照片总数 / 最早照片 / 至少经营多少年。"""
    if not shop_ids:
        print("用法：--probe <店铺ID> [店铺ID…]")
        return 2
    settings = SettingsManager()
    http = HTTPClient(settings)
    discovery = ShopDiscovery(http, settings, logger=lambda m: None)
    print(f"{'店铺ID':<20} {'至少经营':>8}  店铺名称 / 城市 / 照片数 / 最早照片")
    print("-" * 100)
    failed = 0
    for shop_id in shop_ids:
        info = discovery.probe_shop(shop_id)
        if info["error"]:
            failed += 1
            print(f"{shop_id:<20} {'-':>8}  失败：{info['error']}")
            continue
        years = info["estimated_years"]
        print(
            f"{shop_id:<20} "
            f"{(f'{years:.0f} 年' if years is not None else '-'):>8}  "
            f"{info['shop_name']} / {info['city']} / "
            f"{info['photo_count']} 张 / 最早 {info['oldest_photo_at'] or '?'}"
        )
    return 1 if failed and failed == len(shop_ids) else 0


def cli_discover(paths, city_keyword="", min_years=0, min_photos=0, limit=300):
    """从浏览器另存为的列表页/搜索结果页里自动找店并筛选。"""
    if not paths:
        print("用法：--discover <html 文件…> [--city 上海] [--min-years 15]")
        return 2
    settings = SettingsManager()
    http = HTTPClient(settings)
    discovery = ShopDiscovery(
        http, settings, db=None, logger=lambda m: None,
    )
    results, stats = discovery.discover_from_files(
        paths, source="CLI",
        city_keyword=city_keyword, min_years=min_years,
        min_photos=min_photos, limit=limit,
    )
    print(f"核验 {stats['checked']}/{stats['total']} 家，"
          f"符合条件 {stats['passed']} 家")
    for reason, count in sorted(
        stats.get("reasons", {}).items(), key=lambda x: -x[1]
    ):
        print(f"  跳过 {count} 家：{reason}")
    print("-" * 100)
    for info in results:
        print(
            f"{info['shop_id']:<20} 至少 {info['estimated_years']:>5.0f} 年  "
            f"{info['shop_name']} / {info['city']} / {info['photo_count']} 张 / "
            f"最早 {info['oldest_photo_at']}"
        )
    return 0


def cli_discover_url(url, cookie="", curl_file="", max_pages=3,
                     city_keyword="", min_years=0, min_photos=0, limit=300):
    """
    带登录态自动翻页找店。

    Cookie 可以这样拿：已登录的浏览器打开列表页 → F12 → Network → 刷新 →
    点第一个请求 → 右键 →「复制」→「以 cURL 格式复制」，
    然后用 --curl <文件> 传进来（或直接把 Cookie 用 --cookie 传入）。
    """
    if not url:
        print("用法：--discover-url <网址> [--cookie <Cookie> | --curl <cURL 文件>] "
              "[--max-pages 3] [--city 上海] [--min-years 15]")
        return 2

    settings = SettingsManager()
    cookie = cookie or ""
    if curl_file:
        text = read_text_file(curl_file)
        if text is None:
            print(f"无法读取 cURL 文件：{curl_file}")
            return 2
        parsed = parse_curl_command(text)
        cookie = parsed.get("cookie") or cookie
        if parsed.get("user_agent"):
            settings.set("user_agent", parsed["user_agent"])
        if parsed.get("url") and not url:
            url = parsed["url"]
    if cookie:
        settings.set("cookie", " ".join(str(cookie).split()))
        print(f"已带上登录态（{len(cookie)} 字符）")
    else:
        print("警告：没有 Cookie，列表页很可能会跳到验证中心")

    http = HTTPClient(settings)
    discovery = ShopDiscovery(http, settings, db=None, logger=lambda m: None)
    results, stats = discovery.discover_from_url(
        url, max_pages=max_pages, source="CLI",
        city_keyword=city_keyword, min_years=min_years,
        min_photos=min_photos, limit=limit,
    )
    if stats.get("error"):
        print(f"中止：{stats['error']}")
    print(f"核验 {stats['checked']}/{stats['total']} 家，"
          f"符合条件 {stats['passed']} 家")
    for reason, count in sorted(
        stats.get("reasons", {}).items(), key=lambda x: -x[1]
    ):
        print(f"  跳过 {count} 家：{reason}")
    print("-" * 100)
    for info in results:
        print(
            f"{info['shop_id']:<20} 至少 {info['estimated_years']:>5.0f} 年  "
            f"{info['shop_name']} / {info['city']} / {info['photo_count']} 张 / "
            f"最早 {info['oldest_photo_at']}"
        )
    return 0


def _cli_login_settings(cookie="", curl_file=""):
    """命令行里从 --cookie / --curl 准备登录态，返回 (settings, cookie, url)。"""
    settings = SettingsManager()
    url = ""
    if curl_file:
        text = read_text_file(curl_file)
        if text is None:
            print(f"无法读取 cURL 文件：{curl_file}")
            return settings, "", ""
        parsed = parse_curl_command(text)
        cookie = parsed.get("cookie") or cookie
        url = parsed.get("url") or ""
        if parsed.get("user_agent"):
            settings.set("user_agent", parsed["user_agent"])
    if cookie:
        settings.set("cookie", " ".join(str(cookie).split()))
    return settings, cookie, url


def cli_list_url(city, category="", sort="", page=1):
    """按语法拼出列表页地址（例：上海 + 餐饮 + 评价最多）。"""
    if not city:
        print("用法：--list-url <城市（中文或拼音）> [--category ch10|美食] "
              "[--sort o11|评价最多] [--page N]")
        return 2
    pinyin = city_pinyin(city)
    if not pinyin:
        print(f"认不出城市「{city}」：可以填中文名，或直接填点评地址里的拼音"
              f"（例如 chaozhou）")
        return 2
    url = build_list_url(pinyin, category, sort=sort, page=page)
    parsed = parse_list_url(url)
    print(url)
    print(
        f"  城市 {city_label(pinyin) or pinyin}（{pinyin}） | "
        f"分类 {parsed['category'] or '默认'} | "
        f"排序 {parsed['sort'] or '默认'} | 第 {parsed['page']} 页"
    )
    return 0


def cli_learn_url(url="", html_file="", cookie="", curl_file=""):
    """
    学习列表页的「城市 / 分类 / 二级分类 / 商圈 / 排序」对应关系。

    --learn-url 需要登录态（列表页有风控，配 --curl 或 --cookie）；
    --learn-html 用浏览器另存为的文件，不需要登录态。
    """
    if not url and not html_file:
        print("用法：--learn-url <列表页网址> [--curl CopyAsCurl.txt | --cookie \"...\"]")
        print("      --learn-html <浏览器另存为的.html> [--url <该页网址>]")
        return 2

    settings, cookie, curl_url = _cli_login_settings(cookie, curl_file)
    if not url:
        url = curl_url
    http = HTTPClient(settings)
    discovery = ShopDiscovery(http, settings, db=None, logger=lambda m: None)

    if html_file:
        html = read_text_file(html_file)
        if html is None:
            print(f"无法读取文件：{html_file}")
            return 2
        learned, error = discovery.learn_filters(html=html, base_url=url)
    else:
        if not cookie:
            print("提示：没有登录态，列表页很可能被风控拦下")
        learned, error = discovery.learn_filters(url=url)

    if error:
        print(f"学习中止：{error}")
    print(
        f"城市 {len(learned.get('cities') or [])} 个 | "
        f"一级分类 {len(learned.get('categories') or [])} 个 | "
        f"二级分类 {len(learned.get('groups') or [])} 个 | "
        f"商圈 {len(learned.get('regions') or [])} 个 | "
        f"排序 {len(learned.get('sorts') or [])} 个"
    )
    for key, title, limit_rows in (
        ("categories", "一级分类", 40), ("sorts", "排序方式", 20),
        ("groups", "二级分类", 20), ("regions", "商圈", 20),
        ("cities", "城市", 30),
    ):
        items = learned.get(key) or []
        if not items:
            continue
        print(f"\n{title}（共 {len(items)} 个，列前 {min(limit_rows, len(items))} 个）：")
        for item in items[:limit_rows]:
            if key == "cities":
                print(f"  {item.get('label', ''):<10} {item.get('pinyin', '')}")
            else:
                print(
                    f"  {item.get('label', ''):<16} {item.get('code', ''):<8} "
                    f"{item.get('url', '')}"
                )
    return 0 if not error else 1


def cli_discover_city(city, category="", sort="", max_pages=2,
                      max_categories=0, cookie="", curl_file="",
                      city_keyword="", min_years=0, min_photos=0, limit=300,
                      delay=None):
    """大范围搜索：按城市扫描分类页，收集店铺后按城市/经营年限筛选。"""
    if not city:
        print("用法：--discover-city <城市> [--category ch10|美食] [--sort o11] "
              "[--max-pages 2] [--max-categories 3] [--city 上海] [--min-years 15]")
        return 2
    settings, cookie_ok, _ = _cli_login_settings(cookie, curl_file)
    if delay is not None:
        settings.set("discover_delay", delay)
    if not cookie_ok:
        print("提示：没有登录态，列表页很可能被风控拦下（建议加 --curl）")

    http = HTTPClient(settings)
    discovery = ShopDiscovery(http, settings, db=None, logger=print)
    results, stats = discovery.discover_city(
        city, category=category, sort=sort,
        max_pages=max_pages, max_categories=max_categories,
        source="CLI", city_keyword=city_keyword, min_years=min_years,
        min_photos=min_photos, limit=limit,
    )
    if stats.get("error"):
        print(f"中止：{stats['error']}")
    print(
        f"核验 {stats['checked']}/{stats['total']} 家，符合条件 {stats['passed']} 家"
        + (f"，限流 {stats['rate_limited']} 次" if stats.get("rate_limited") else "")
    )
    for reason, count in sorted(
        stats.get("reasons", {}).items(), key=lambda x: -x[1]
    ):
        print(f"  跳过 {count} 家：{reason}")
    print("-" * 100)
    for info in results:
        print(
            f"{info['shop_id']:<20} 至少 {info['estimated_years']:>5.0f} 年  "
            f"{info['shop_name']} / {info['city']} / {info['photo_count']} 张 / "
            f"最早 {info['oldest_photo_at']}"
        )
    return 0


def run_gui():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass

    settings = SettingsManager()

    app = DianpingArchiveGUI(root, settings)

    def on_close():
        try:
            if app.controller:
                app.controller.stop()
            if app.db:
                app.db.close()
            if app.browser:
                app.browser.destroy()
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return 0


def main(argv=None):
    # 中文 Windows 控制台是 GBK，先做好编码兜底，免得打印 ✓ / 生僻字把命令打断
    make_console_encoding_safe()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        command = argv[0].lower()
        if command in ("--parse-html", "-p", "--parse"):
            return cli_parse_html(argv[1] if len(argv) > 1 else "")
        if command in ("--selftest", "-t", "--test"):
            return cli_selftest()
        if command in ("--check-deps", "--deps"):
            return cli_check_deps()
        if command == "--probe":
            return cli_probe([x for x in argv[1:] if not x.startswith("-")])
        if command == "--list-url":
            options = {"city": "", "category": "", "sort": "", "page": 1}
            index = 1
            while index < len(argv):
                item = argv[index]
                value = argv[index + 1] if index + 1 < len(argv) else ""
                if item == "--category":
                    options["category"] = value
                elif item == "--sort":
                    options["sort"] = value
                elif item == "--page":
                    options["page"] = int(value or 1)
                elif not item.startswith("-") and not options["city"]:
                    options["city"] = item
                index += 2 if item.startswith("--") else 1
            return cli_list_url(**options)
        if command in ("--learn-url", "--learn-html"):
            options = {"url": "", "html_file": "", "cookie": "", "curl_file": ""}
            index = 1
            while index < len(argv):
                item = argv[index]
                value = argv[index + 1] if index + 1 < len(argv) else ""
                if item == "--url":
                    options["url"] = value
                elif item == "--cookie":
                    options["cookie"] = value
                elif item == "--curl":
                    options["curl_file"] = value
                elif not item.startswith("-"):
                    if command == "--learn-html":
                        options["html_file"] = item
                    elif not options["url"]:
                        options["url"] = item
                index += 2 if item.startswith("--") else 1
            return cli_learn_url(**options)
        if command == "--discover-city":
            options = {
                "city": "", "category": "", "sort": "", "max_pages": 2,
                "max_categories": 0, "cookie": "", "curl_file": "",
                "city_keyword": "", "min_years": 0, "min_photos": 0,
                "limit": 300, "delay": None,
            }
            flags = {
                "--category": ("category", str), "--sort": ("sort", str),
                "--max-pages": ("max_pages", int),
                "--max-categories": ("max_categories", int),
                "--cookie": ("cookie", str), "--curl": ("curl_file", str),
                "--city": ("city_keyword", str),
                "--min-years": ("min_years", int),
                "--min-photos": ("min_photos", int),
                "--limit": ("limit", int), "--delay": ("delay", float),
            }
            index = 1
            while index < len(argv):
                item = argv[index]
                if item in flags and index + 1 < len(argv):
                    key, caster = flags[item]
                    options[key] = caster(argv[index + 1])
                    index += 2
                elif not item.startswith("-") and not options["city"]:
                    options["city"] = item
                    index += 1
                else:
                    index += 1
            return cli_discover_city(**options)
        if command == "--discover-url":
            options = {
                "url": "", "cookie": "", "curl_file": "", "max_pages": 3,
                "city_keyword": "", "min_years": 0, "min_photos": 0,
                "limit": 300,
            }
            index = 1
            while index < len(argv):
                item = argv[index]
                value = argv[index + 1] if index + 1 < len(argv) else ""
                if item in ("--cookie", "--curl", "--max-pages", "--city",
                            "--min-years", "--min-photos", "--limit"):
                    if not value:
                        index += 1
                        continue
                    if item == "--cookie":
                        options["cookie"] = value
                    elif item == "--curl":
                        options["curl_file"] = value
                    elif item == "--max-pages":
                        options["max_pages"] = int(value)
                    elif item == "--city":
                        options["city_keyword"] = value
                    elif item == "--min-years":
                        options["min_years"] = int(value)
                    elif item == "--min-photos":
                        options["min_photos"] = int(value)
                    elif item == "--limit":
                        options["limit"] = int(value)
                    index += 2
                else:
                    if not options["url"]:
                        options["url"] = item
                    index += 1
            return cli_discover_url(**options)
        if command == "--discover":
            options = {
                "city_keyword": "", "min_years": 0,
                "min_photos": 0, "limit": 300,
            }
            files = []
            index = 1
            while index < len(argv):
                item = argv[index]
                if item == "--city" and index + 1 < len(argv):
                    options["city_keyword"] = argv[index + 1]
                    index += 2
                elif item == "--min-years" and index + 1 < len(argv):
                    options["min_years"] = int(argv[index + 1])
                    index += 2
                elif item == "--min-photos" and index + 1 < len(argv):
                    options["min_photos"] = int(argv[index + 1])
                    index += 2
                elif item == "--limit" and index + 1 < len(argv):
                    options["limit"] = int(argv[index + 1])
                    index += 2
                else:
                    files.append(item)
                    index += 1
            return cli_discover(files, **options)
        if command in ("-h", "--help", "help"):
            print(USAGE)
            return 0
        print(f"未知参数：{argv[0]}")
        print(USAGE)
        return 2
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
