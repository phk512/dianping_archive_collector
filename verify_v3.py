"""
verify_v3.py —— 验证 dianping_archive_collector_v3_2.py 对「上传者 / 发布时间」的修复。

用法：
    python verify_v3.py           只跑离线用例（不需要网络，结果稳定可重复）
    python verify_v3.py --live    额外抓取真实相册页做结构性校验（只用数量类断言，
                                  不写死具体用户名/日期，所以不会因为页面新增图片而失败）

依赖：beautifulsoup4（缺少时脚本会给出提示），联网用例另需能访问 www.dianping.com。
"""
import importlib.util
import re
import sqlite3
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "dianping_archive_collector_v3_2.py"
LIVE = "--live" in sys.argv

passed, failed = [], []


def check(condition, label, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------- 载入被测脚本
for name in ("requests", "imagehash"):
    try:
        __import__(name)
    except Exception:
        module = types.ModuleType(name)
        module.hex_to_hash = lambda *a, **k: None
        module.phash = lambda *a, **k: None
        module.Session = object
        sys.modules[name] = module

source = SCRIPT.read_text(encoding="utf-8")

section("1. 语法与导入")
try:
    compile(source, str(SCRIPT), "exec")
    check(True, "整文件 compile() 通过（无语法错误）")
except SyntaxError as exc:
    check(False, "整文件 compile() 通过", str(exc))
    print(f"\n失败 {len(failed)} 项"); sys.exit(1)

spec = importlib.util.spec_from_file_location("dianping_v3", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules["dianping_v3"] = mod
spec.loader.exec_module(mod)
check(True, "模块导入成功")
check(mod.APP_VERSION.startswith("3."), f"版本号 = {mod.APP_VERSION}")
check(mod.BS4_AVAILABLE, f"beautifulsoup4 可用 = {mod.BS4_AVAILABLE}")
if not mod.BS4_AVAILABLE:
    print("  请先执行：pip install beautifulsoup4")

YEAR = datetime.now().year

# ---------------------------------------------------------------- URL 工具
section("2. 图片地址工具")
thumb = ("https://img.meituan.net/ugcpic/"
         "6c08c3006402501fbf08423329cb597f558858.jpg"
         "%40240w_180h_1e_1c_1l%7Cwatermark%3D0")
check(
    mod.original_size_image_url(thumb)
    == "https://img.meituan.net/ugcpic/6c08c3006402501fbf08423329cb597f558858.jpg",
    "去掉 %40 尺寸后缀还原原图",
)
check(
    mod.original_size_image_url("https://a/b.jpg@600w_600h.webp") == "https://a/b.jpg",
    "去掉 @ 尺寸后缀还原原图",
)
plain = "https://qcloud.dpfile.com/pc/abc.jpg"
check(mod.original_size_image_url(plain) == plain, "无后缀地址保持不变")
check(
    mod.image_basename(thumb) == mod.image_basename("https://img.meituan.net/ugcpic/"
                                                    "6c08c3006402501fbf08423329cb597f558858.jpg@600w.webp"),
    "同一张图不同尺寸后缀 -> 相同 basename",
)
check(
    mod.image_basename("https://a/b%20c.jpg") != mod.image_basename("https://a/b.jpg"),
    "%20 编码文件名不会被误截断",
)

# ---------------------------------------------------------------- 日期解析
section("3. 发布时间解析")
for raw, expected in (
    ("25-12-18", "2025-12-18"),
    ("24-06-17", "2024-06-17"),
    ("19-09-24", "2019-09-24"),
    ("2024-07-15", "2024-07-15"),
    ("2019年9月24日", "2019-09-24"),
    ("2024/7/5 13:20", "2024-07-05 13:20"),
    ("2024-07-05 13:20:45", "2024-07-05 13:20:45"),
):
    value, _raw, _inferred, matched = mod.parse_published_at(raw)
    check(value == expected and matched, f"parse({raw!r}) -> {expected}", f"实际 {value!r}")

value, _raw, inferred, matched = mod.parse_published_at("01-15", now=datetime(YEAR, 6, 1))
check(
    value == f"{YEAR}-01-15" and inferred and matched,
    f"当年 MM-DD 补全年份 -> {YEAR}-01-15（点评规则：当年只显示 MM-DD）",
    f"实际 {value!r}",
)
value, _raw, inferred, _m = mod.parse_published_at("12-31", now=datetime(YEAR, 1, 15))
check(value == f"{YEAR - 1}-12-31", "晚于今天的 MM-DD 退一年", f"实际 {value!r}")
value, _raw, inferred, _m = mod.parse_published_at("06-21", now=datetime(YEAR, 6, 1), infer_year=False)
check(value == "06-21" and not inferred, "关闭补全年份时保留 MM-DD 原样", f"实际 {value!r}")
value, raw_out, _i, matched = mod.parse_published_at("25-12-18 报错")
check(value == "2025-12-18" and raw_out == "25-12-18", "卡片文本里取日期正确")
for negative in ("报错", "|", "", "全部图片", "流东向水春江一", "共 123 张", "点击看大图"):
    check(not mod.parse_published_at(negative)[3], f"不把 {negative!r} 当日期")
check(
    mod.parse_published_at("3天前", now=datetime(2026, 9, 24, 10, 0))[0].startswith("2026-09-21"),
    "相对时间 '3天前' 可解析",
)

# ---------------------------------------------------------------- 上传者清洗
section("4. 上传者清洗")
check(mod.clean_uploader("报错") == "" and mod.clean_uploader("全部图片") == "", "页面按钮文本被过滤")
check(mod.clean_uploader("26230") == "", "纯数字被过滤")
check(mod.clean_uploader("流东向水春江一") == "流东向水春江一", "中文用户名保留")
check(mod.clean_uploader("W.") == "W.", "带点号的用户名保留")
check(mod.clean_display_name("🍜吃货") == "🍜吃货", "/member/ 链接来的用户名不做启发式丢弃")

# ---------------------------------------------------------------- 内置自检
section("5. 脚本自带 --selftest")
check(mod.cli_selftest() == 0, "cli_selftest() 返回 0")

# ---------------------------------------------------------------- 回归用例
section("6. 回归用例：不得把邻图的作者/时间安到当前图片")

CARD = """
<li class="J_list">
  <div class="img"><span class="hook"></span>
    <a class="J_entry" href="/photos/{pid}" data-index="{idx}">
      <img src="https://qcloud.dpfile.com/pc/{img}.jpg" title="点击看大图" alt="-某店"></a>
  </div>
  <div class="picture-info">
    <div class="name"><h3><a class="J_entry" href="/photos/{pid}" title=""></a></h3></div>
    <div class="info">{info}</div>
  </div>
</li>
"""
GOOD = ('<a rel="nofollow" href="/member/999" title="{name}">{name}</a>'
        '<em class="sep">|</em><span>{date}</span>')
EMPTY = '<a rel="nofollow" title=""></a><em class="sep">|</em><span></span>'
mixed = (
    '<html><body><ul class="picture-list">'
    + CARD.format(pid="1111", idx="0", img="aaa", info=GOOD.format(name="甲用户", date="25-01-02"))
    + CARD.format(pid="2222", idx="1", img="bbb", info=EMPTY)
    + CARD.format(pid="3333", idx="2", img="ccc", info=GOOD.format(name="丙用户", date="24-03-04"))
    + "</ul></body></html>"
)
parser = mod.DianpingHTMLParser()
urls = parser.extract_image_urls(mixed)
meta = parser.extract_image_metadata(mixed, urls)


def meta_of(img_name):
    return meta.get(mod.canonical_image_url(f"https://qcloud.dpfile.com/pc/{img_name}.jpg"), {}) or {}


check(len(parser.extract_album_cards(mixed)) == 3, "解析出 3 张卡片")
check(
    meta_of("aaa").get("uploader") == "甲用户"
    and meta_of("aaa").get("published_at") == "2025-01-02",
    "第 1 张：作者/时间正确",
)
check(
    not meta_of("bbb").get("uploader") and not meta_of("bbb").get("published_at"),
    "第 2 张（信息缺失）：留空，没有继承邻图的作者/时间",
    f"{meta_of('bbb')}",
)
check(
    meta_of("ccc").get("uploader") == "丙用户"
    and meta_of("ccc").get("published_at") == "2024-03-04",
    "第 3 张：作者/时间正确",
)

weird = ('<html><body><div class="weird"><div class="box">'
         '<img src="https://qcloud.dpfile.com/pc/d1.jpg">'
         '<img src="https://qcloud.dpfile.com/pc/d2.jpg">'
         '<a href="/member/5">某人</a><em class="sep">|</em><span>2023-05-06</span>'
         '</div></div></body></html>')
parser2 = mod.DianpingHTMLParser()
urls2 = parser2.extract_image_urls(weird)
meta2 = parser2.extract_image_metadata(weird, urls2)
check(
    all(not (meta2.get(mod.canonical_image_url(u)) or {}).get("published_at") for u in urls2),
    "多图容器 + 单个日期：不猜归属（全部留空）",
)

sibling = ('<html><body><div class="item">'
           '<div class="thumb"><img src="https://qcloud.dpfile.com/pc/e1.jpg"></div>'
           '<div class="meta"><a href="/member/123" title="小王">小王</a>'
           '<em class="sep">|</em><span>22-11-22</span></div></div></body></html>')
parser3 = mod.DianpingHTMLParser()
urls3 = parser3.extract_image_urls(sibling)
meta3 = parser3.extract_image_metadata(sibling, urls3)
item3 = meta3.get(mod.canonical_image_url(urls3[0]), {}) if urls3 else {}
check(
    item3.get("uploader") == "小王" and item3.get("published_at") == "2022-11-22",
    "未知版式：作者/时间在兄弟节点也能配对",
)

saved = (mod.BS4_AVAILABLE, mod.BS4_ERROR)
try:
    mod.BS4_AVAILABLE, mod.BS4_ERROR = False, "模拟缺失"
    parser4 = mod.DianpingHTMLParser()
    meta4 = parser4.extract_image_metadata(mixed, urls)
    diagnostics = parser4.last_diagnostics
    check("beautifulsoup4" in (diagnostics.get("error") or ""), "缺少 bs4 时给出明确错误，而不是静默出错")
    check(all(not item.get("published_at") for item in meta4.values()), "缺少 bs4 时不写入任何猜测值")
finally:
    mod.BS4_AVAILABLE, mod.BS4_ERROR = saved

# ---------------------------------------------------------------- 数据库
section("7. 数据库：新字段 / 置信度覆盖 / 旧库升级")
with tempfile.TemporaryDirectory() as tmp:
    db = mod.ArchiveDB(str(Path(tmp) / "archive.db"))
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(images)").fetchall()}
    for column in ("photo_id", "photo_index", "published_raw", "year_inferred", "metadata_confidence"):
        check(column in columns, f"images 表新增列 {column}")

    image_id = db.add_image(
        shop_id="s1", page_no=1, image_url="http://x/a.jpg",
        canonical_url="http://x/a.jpg", local_path="a.jpg", sha256="sha1",
        phash="p1", width=100, height=100, filesize=10,
        uploader="错的作者", published_at="2020-01-01",
        metadata_source="card+card_sep", published_raw="20-01-01",
        metadata_confidence=30, photo_id="111", photo_index=3,
    )
    db.update_image_metadata(
        image_id, uploader="对的作者", published_at="2025-12-18",
        metadata_source="card+card_sep", published_raw="25-12-18",
        metadata_confidence=100,
    )
    row = db.conn.execute(
        "SELECT uploader, published_at, metadata_confidence FROM images WHERE id=?",
        (image_id,),
    ).fetchone()
    check(row == ("对的作者", "2025-12-18", 100), "高置信度可以纠正旧值（V2.9 永远改不回来）", f"{row}")

    db.update_image_metadata(
        image_id, uploader="低置信度猜测", published_at="1999-01-01",
        metadata_source="heuristic+card_text", metadata_confidence=45,
    )
    db.update_image_metadata(image_id, uploader="", published_at="", metadata_confidence=90)
    row = db.conn.execute("SELECT uploader, published_at FROM images WHERE id=?", (image_id,)).fetchone()
    check(row == ("对的作者", "2025-12-18"), "低置信度/空值不会覆盖已有正确值", f"{row}")

    found = db.find_by_canonical_url("http://x/a.jpg")
    check(found is not None and found[0] == image_id, "按 canonical_url 查得到记录")
    images = db.get_images("s1", 1)
    check(all(len(item) == 19 for item in images), "get_images 返回 19 列（与界面解包一致）")
    stats = db.stats()
    check(
        stats["with_uploader"] == 1 and stats["with_published"] == 1,
        "stats() 新增作者/时间统计",
        f"{stats}",
    )
    db.close()

    legacy_path = str(Path(tmp) / "legacy.db")
    legacy = sqlite3.connect(legacy_path)
    legacy.executescript(
        """
        CREATE TABLE images (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id TEXT, page_no INTEGER,
            image_url TEXT, canonical_url TEXT, local_path TEXT, sha256 TEXT,
            phash TEXT, width INTEGER DEFAULT 0, height INTEGER DEFAULT 0,
            filesize INTEGER DEFAULT 0, uploader TEXT DEFAULT '',
            published_at TEXT DEFAULT '', metadata_source TEXT DEFAULT '',
            duplicate_of INTEGER, status TEXT, created_at REAL
        );
        INSERT INTO images (shop_id, page_no, image_url, canonical_url, local_path,
                            uploader, published_at, status)
        VALUES ('s9', 1, 'http://x/c.jpg', 'http://x/c.jpg', 'c.jpg',
                '旧作者', '20-06-01', 'downloaded');
        """
    )
    legacy.commit()
    legacy.close()
    upgraded = mod.ArchiveDB(legacy_path)
    columns = {row[1] for row in upgraded.conn.execute("PRAGMA table_info(images)").fetchall()}
    check(
        {"photo_id", "published_raw", "year_inferred", "metadata_confidence"} <= columns,
        "旧版 archive.db 自动补列",
    )
    old_id = upgraded.conn.execute("SELECT id FROM images WHERE shop_id='s9'").fetchone()[0]
    upgraded.update_image_metadata(
        old_id, uploader="新作者", published_at="2025-12-18",
        metadata_source="card+card_sep", published_raw="25-12-18",
        metadata_confidence=100,
    )
    row = upgraded.conn.execute("SELECT uploader, published_at FROM images WHERE id=?", (old_id,)).fetchone()
    check(row == ("新作者", "2025-12-18"), "旧数据可以被重爬结果修正", f"{row}")
    upgraded.close()

# ---------------------------------------------------------------- 联网校验
if LIVE:
    section("8. 真实页面结构性校验（联网）")
    import urllib.request

    PAGES = (("9952743", 1), ("H3Rq9hMRwjna1wNp", 1), ("102451540", 1))
    for shop_id, page_no in PAGES:
        url = f"https://www.dianping.com/shop/{shop_id}/photos?pg={page_no}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": mod.USER_AGENT, "Referer": "https://www.dianping.com/"},
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                html = response.read().decode("utf-8", "replace")
        except Exception as exc:
            check(False, f"{shop_id}/pg{page_no} 抓取成功", f"{type(exc).__name__}: {exc}")
            continue

        parser = mod.DianpingHTMLParser()
        cards = parser.extract_album_cards(html)
        urls = parser.extract_image_urls(html)
        assets = parser.extract_photo_assets(html)
        metadata = parser.extract_image_metadata(html, urls)
        diagnostics = parser.last_diagnostics

        check(len(cards) == 16, f"{shop_id} 解析出 16 张卡片", f"实际 {len(cards)}")
        check(len(urls) == 16, f"{shop_id} 图片地址 16 个（不含 JSON 变体/脚本）", f"实际 {len(urls)}")
        check(len(assets) == 16, f"{shop_id} 内嵌原图数据 16 条", f"实际 {len(assets)}")
        check(
            set(assets) == {c["photo_id"] for c in cards if c["photo_id"]},
            f"{shop_id} 内嵌 picId 与卡片 /photos/<id> 一一对应",
        )
        check(not [u for u in urls if re.search(r"\.(?:js|css|json)(?:$|[?#])", u, re.I)],
              f"{shop_id} 结果中无 js/css 资源")
        check(
            diagnostics.get("cards_with_date") == 16,
            f"{shop_id} 16/16 张都有发布时间",
            f"实际 {diagnostics.get('cards_with_date')}",
        )
        member_links = len(re.findall(r'href="/member/\d+"', html))
        check(
            diagnostics.get("cards_with_uploader") == member_links,
            f"{shop_id} 作者数与页面 /member/ 链接数一致（= {member_links}）",
            f"实际 {diagnostics.get('cards_with_uploader')}",
        )
        missing = [
            u for u in urls
            if not (metadata.get(mod.canonical_image_url(u)) or {}).get("published_at")
        ]
        check(not missing, f"{shop_id} 每张图片都绑定了元数据", f"未绑定 {len(missing)}")

        first_url = cards[0]["image_urls"][0]
        candidates = mod.image_download_candidates(
            first_url, cards[0]["photo_id"], assets
        )
        check(
            len(candidates) >= 2 and candidates[0] != first_url,
            f"{shop_id} 下载候选优先于 240x180 缩略图",
            f"{[c[-30:] for c in candidates]}",
        )

    # ---- 旧数据修复流程 ----
    section("9. 旧数据修复流程：重采已完成页面，把写错的作者/时间改回来")
    import shutil

    shop_id = "9952743"

    class NoDownloadHTTP(mod.HTTPClient):
        """只抓页面、不下载图片，用来证明修复过程没有重新下载。"""

        def __init__(self, settings):
            super().__init__(settings)
            self.image_calls = []

        def get_bytes(self, url, referer=None):
            self.image_calls.append(url)
            return None

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        settings = mod.SettingsManager(path=str(work / "settings.json"))
        settings.set("image_delay", 0)
        settings.set("page_delay", 0)
        settings.set("recrawl_completed_pages", False)

        db = mod.ArchiveDB(str(work / "archive.db"))
        http = NoDownloadHTTP(settings)
        engine = mod.DianpingArchiveEngine(
            db=db, http_client=http, resolver=mod.DianpingURLResolver(http),
            settings=settings, logger=lambda message: None,
        )

        html = http.get_html(
            f"https://www.dianping.com/shop/{shop_id}/photos?pg=1"
        ).text
        urls = engine.parser.extract_image_urls(html)
        correct = engine.parser.extract_image_metadata(html, urls)
        if not urls:
            check(False, "修复流程用例：取到真实页面")
        else:
            # 造一整页旧版写错的数据
            db.upsert_shop(shop_id=shop_id)
            for index, url in enumerate(urls):
                db.add_image(
                    shop_id=shop_id, page_no=1, image_url=url,
                    canonical_url=mod.canonical_image_url(url),
                    local_path=f"old_{index}.jpg", sha256=f"old{index}",
                    phash="old", width=240, height=180, filesize=100,
                    uploader=f"隔壁照片的作者{index}",
                    published_at="2019-01-01", metadata_source="html_window",
                    metadata_confidence=0, status="downloaded",
                )
            db.upsert_page(
                shop_id, 1,
                f"https://www.dianping.com/shop/{shop_id}/photos?pg=1",
                status="completed", image_count=len(urls),
                downloaded_count=len(urls),
            )

            def wrong_rows():
                return db.conn.execute(
                    "SELECT COUNT(*) FROM images WHERE "
                    "uploader LIKE '隔壁照片的作者%' OR published_at='2019-01-01'"
                ).fetchone()[0]

            engine.crawl_shop(shop_id, str(work / "out"), 1, 1)
            check(wrong_rows() == 16, "默认不会重采已完成页面", f"错误行 {wrong_rows()}")

            settings.set("recrawl_completed_pages", True)
            engine.crawl_shop(shop_id, str(work / "out"), 1, 1)
            check(wrong_rows() == 0, "重采后整页错误元数据全部纠正", f"剩余 {wrong_rows()} 行")
            check(
                http.image_calls == [],
                "修复过程没有重新下载任何图片",
                f"下载调用 {len(http.image_calls)} 次",
            )
            mismatched = 0
            for key, uploader, published, raw, photo_id, confidence in db.conn.execute(
                "SELECT canonical_url, COALESCE(uploader,''), "
                "COALESCE(published_at,''), COALESCE(published_raw,''), "
                "COALESCE(photo_id,''), COALESCE(metadata_confidence,0) FROM images"
            ).fetchall():
                item = correct.get(key) or {}
                if (
                    uploader != item.get("uploader")
                    or published != item.get("published_at")
                    or raw != item.get("published_raw")
                    or photo_id != item.get("photo_id")
                    or confidence != item.get("confidence")
                ):
                    mismatched += 1
            check(
                mismatched == 0,
                "16 条记录与新解析结果完全一致",
                f"不一致 {mismatched} 条",
            )
        db.close()

    shutil.rmtree(work, ignore_errors=True)
else:
    section("8. 真实页面结构性校验（已跳过）")
    print("  加 --live 参数可联网校验真实相册页")

# ---------------------------------------------------------------- 汇总
section("汇总")
print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
for item in failed:
    print(f"  FAIL: {item}")
sys.exit(1 if failed else 0)
