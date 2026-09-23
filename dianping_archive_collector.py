import os
import re
import csv
import math
import time
import json
import queue
import hashlib
import threading
import sqlite3
import webbrowser
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
from PIL import Image, ImageTk, ImageOps
import imagehash

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
}


SETTINGS_FILE = "dianping_settings.json"
APP_NAME = "Dianping Archive Collector V2.7"

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
# HTTP
# ============================================================

class HTTPClient:

    def __init__(self, settings):
        self.settings = settings
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

    def get_html(self, url):
        try:
            timeout = self.settings.get("request_timeout", 15)
            r = self.session.get(
                url, timeout=timeout, allow_redirects=True
            )
            r.encoding = r.apparent_encoding or "utf-8"
            return r
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

    def extract_image_urls(self, html):
        if not html:
            return []

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
            url = url.strip()
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            if not re.match(r"^https?://", url, flags=re.I):
                continue
            url = normalize_url(url)
            canonical = canonical_image_url(url)
            if canonical in seen:
                continue
            seen.add(canonical)
            result.append(url)

        return result


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
            self.conn.commit()

    def close(self):
        with self.lock:
            try:
                self.conn.commit()
                self.conn.close()
            except Exception:
                pass

    def upsert_shop(self, shop_id, shop_name=None, city=None,
                    source_url=None, resolved_url=None):
        now = time.time()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO shops (
                    shop_id, shop_name, city, source_url,
                    resolved_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(shop_id) DO UPDATE SET
                    shop_name=COALESCE(excluded.shop_name, shop_name),
                    city=COALESCE(excluded.city, city),
                    source_url=COALESCE(excluded.source_url, source_url),
                    resolved_url=COALESCE(excluded.resolved_url, resolved_url),
                    updated_at=excluded.updated_at
                """,
                (shop_id, shop_name, city, source_url, resolved_url, now, now),
            )
            self.conn.commit()

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
                    COUNT(DISTINCT images.id)
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
                SELECT shop_id, shop_name, city, source_url, resolved_url
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
                  duplicate_of=None, status="downloaded"):
        with self.lock:
            cur = self.conn.execute(
                """
                INSERT INTO images (
                    shop_id, page_no, image_url, canonical_url,
                    local_path, sha256, phash, width, height,
                    filesize, duplicate_of, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (shop_id, page_no, image_url, canonical_url,
                 local_path, sha256, phash, width, height,
                 filesize, duplicate_of, status, time.time()),
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
                       width, height, filesize
                FROM images
                WHERE canonical_url=?
                LIMIT 1
                """,
                (canonical_url,),
            ).fetchone()

    def update_image_content(self, old_sha, new_sha, new_phash,
                             new_width, new_height,
                             new_filesize, new_local_path):
        if not old_sha:
            return
        with self.lock:
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
                    filesize=?, local_path=?
                WHERE sha256=?
                """,
                (new_sha, new_phash, new_width, new_height,
                 new_filesize, new_local_path, old_sha),
            )
            self.conn.commit()

    def get_images(self, shop_id, page_no=None):
        with self.lock:
            if page_no is None:
                return self.conn.execute(
                    """
                    SELECT id, page_no, image_url, local_path, sha256,
                           phash, width, height, filesize, duplicate_of, status
                    FROM images WHERE shop_id=?
                    ORDER BY page_no DESC, id ASC
                    """,
                    (shop_id,),
                ).fetchall()
            return self.conn.execute(
                """
                SELECT id, page_no, image_url, local_path, sha256,
                       phash, width, height, filesize, duplicate_of, status
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
            return {
                "shops": shops, "pages": pages, "images": images,
                "unique_images": unique_images, "duplicates": duplicates,
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
        self.parser = DianpingHTMLParser()
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
        replaced = 0

        manifest_path = page_dir / "manifest.csv"
        manifest_exists = manifest_path.exists()

        manifest_file = open(manifest_path, "a", newline="", encoding="utf-8-sig")
        writer = csv.writer(manifest_file)

        if not manifest_exists:
            writer.writerow([
                "page", "index", "image_url", "canonical_url",
                "local_path", "sha256", "phash", "width", "height",
                "filesize", "duplicate_of", "status",
            ])

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

            # ---------- 第 2 层：URL 预去重 ----------
            if enable_url_dedup:
                existing = self.db.find_by_canonical_url(canonical_url)
                if existing:
                    existing_id = existing[0]
                    existing_path = existing[1]
                    writer.writerow([
                        page_no, index, image_url, canonical_url,
                        existing_path or "", "", "", 0, 0, 0,
                        existing_id, "skipped_url_exists",
                    ])
                    manifest_file.flush()
                    skipped_url += 1
                    continue

            self.logger(f"[IMAGE] {index}/{len(image_urls)}")

            data = self.http.get_bytes(image_url, referer=url)
            if not data:
                writer.writerow([
                    page_no, index, image_url, canonical_url,
                    "", "", "", 0, 0, 0, "", "download_failed",
                ])
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
                    "", f"skipped_too_small({width}x{height})",
                ])
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
                )
                writer.writerow([
                    page_no, index, image_url, canonical_url,
                    duplicate["local_path"] or "",
                    sha, phash or "", width, height, filesize,
                    duplicate_id, "duplicate_sha256",
                ])
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
                        "", "replaced_higher_quality",
                    ])
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
                    )
                    writer.writerow([
                        page_no, index, image_url, canonical_url,
                        duplicate["local_path"] or "",
                        sha, phash or "", width, height, filesize,
                        duplicate_id, "duplicate_phash",
                    ])
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
            )

            writer.writerow([
                page_no, index, image_url, canonical_url,
                str(local_path), sha, phash or "",
                width, height, filesize, "", "downloaded",
            ])
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

        if removed_in_page or skipped_url or skipped_size or replaced:
            self.logger(
                f"[PAGE] 第 {page_no} 页汇总："
                f"页内去重 {removed_in_page}，"
                f"URL 重复跳过 {skipped_url}，"
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

            if self.db.page_status(shop_id, page_no) == "completed":
                self.logger(f"[SKIP] 第 {page_no} 页已完成")
                continue

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
              start_page=None, stop_page=None, photo_count=None):
        if self.running:
            self.logger("[CTRL] 当前已经在运行")
            return
        self.pause_event.clear()
        self.stop_event.clear()
        self.running = True
        self.thread = threading.Thread(
            target=self._worker,
            args=(input_url, output_dir, mode,
                  start_page, stop_page, photo_count),
            daemon=True,
        )
        self.thread.start()

    def _worker(self, input_url, output_dir, mode,
                start_page, stop_page, photo_count):
        try:
            extracted = extract_dianping_url(input_url)
            if not extracted:
                self.logger("[ERROR] 输入中未找到有效的大众点评链接。")
                return

            self.logger(f"[URL] 提取到：{extracted}")

            result = self.resolver.resolve(extracted)
            self.logger(f"[URL] 类型：{result['type']}")
            self.logger(f"[URL] 最终地址：{result['final_url']}")

            shop_id = result.get("shop_id")
            if not shop_id:
                self.logger("[ERROR] 无法解析出 shop_id。")
                return

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

    def refresh_shops(self):
        for item in self.shop_tree.get_children():
            self.shop_tree.delete(item)
        keyword = self.search_var.get().strip().lower()
        shops = self.db.get_shops()
        city_nodes = {}

        for (shop_id, shop_name, city, page_count, image_count) in shops:
            display = f"{shop_name or '未命名商户'} [{shop_id}]"
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
                f"重复 {stats['duplicates']}"
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
            (shop_id, shop_name, city, source_url, resolved_url) = shop
            self.info_label.config(
                text=(
                    f"{city or '未知城市'}  |  "
                    f"{shop_name or '未命名商户'}  |  "
                    f"Shop ID: {shop_id}"
                )
            )
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
        images = self.db.get_images(shop_id, page_no)
        row = 0
        col = 0

        thumb_w = self.settings.get("thumb_w", 150)
        thumb_h = self.settings.get("thumb_h", 120)

        for image_row in images:
            (image_id, page, image_url, local_path, sha, phash,
             width, height, filesize, duplicate_of, status) = image_row
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
                caption = f"{name[:18]}\n{width}×{height}"
                text_label = ttk.Label(
                    card, text=caption, width=22, anchor="center"
                )
                text_label.pack(padx=3, pady=(0, 3))

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

    def add_bool_field(self, parent, key, label, tooltip=""):
        var = tk.BooleanVar()
        cb = ttk.Checkbutton(parent, text=label, variable=var)
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
            except Exception:
                errors.append(f"{key}: {raw}")

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

        self.create_ui()
        self.root.after(100, self.flush_logs)
        self.ensure_database()

    def create_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)

        self.collect_tab = ttk.Frame(notebook)
        notebook.add(self.collect_tab, text="采集")
        self.create_collect_ui()

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

def main():
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


if __name__ == "__main__":
    main()
