"""验证三个新功能：采集日期范围 / 自动获取商户名称 / 自动找店。"""
import csv
import importlib.util
import re
import shutil
import sys
import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "dianping_archive_collector_v3_2.py"
spec = importlib.util.spec_from_file_location("dianping_v3", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules["dianping_v3"] = mod
spec.loader.exec_module(mod)

passed, failed = [], []


def check(condition, label, detail=""):
    (passed if condition else failed).append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def section(title):
    print("\n" + "=" * 84)
    print(title)
    print("=" * 84)


def make_jpeg(size=420, color="blue"):
    buffer = BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="JPEG")
    return buffer.getvalue()


JPEG = make_jpeg()


class CountingHTTP(mod.HTTPClient):
    """真实联网，但统计请求次数（用于证明末页发现只需要 2 次请求）。"""

    def __init__(self, settings, fake_images=True):
        super().__init__(settings)
        self.html_calls = []
        self.image_calls = []
        self.fake_images = fake_images

    def get_html(self, url):
        self.html_calls.append(url)
        return super().get_html(url)

    def get_bytes(self, url, referer=None):
        self.image_calls.append(url)
        return JPEG if self.fake_images else None


# ================================================================
section("1. 日期范围：输入规整与判断")
for raw, expected in (
    ("2010", "2010-01-01"),
    ("2010-06", "2010-06-01"),
    ("2010-06-15", "2010-06-15"),
    ("2010/6/5", "2010-06-05"),
    ("2010年6月15日", "2010-06-15"),
    ("", ""),
    ("不是日期", ""),
    ("  2015-03  ", "2015-03-01"),
):
    got = mod.normalize_date_bound(raw)
    check(got == expected, f"normalize_date_bound({raw!r}) = {expected!r}", f"实际 {got!r}")

for value, date_from, date_to, expected in (
    ("2026-06-21", "2026-01-01", "2026-12-31", True),
    ("2026-06-21", "2026-07-01", "", False),
    ("2026-06-21", "", "2026-06-20", False),
    ("2026-06-21", "2026-06-21", "2026-06-21", True),
    ("06-21", "2026-01-01", "", None),
    ("", "", "", None),
):
    got = mod.date_in_range(value, date_from, date_to)
    check(
        got is expected,
        f"date_in_range({value!r}, {date_from!r}, {date_to!r}) = {expected}",
        f"实际 {got}",
    )

years = mod.estimate_years("2011-06-15", now=datetime(2026, 6, 15))
check(years == 15.0, "estimate_years('2011-06-15') = 15.0", f"实际 {years}")
check(mod.estimate_years("") is None, "estimate_years('') = None")
check(
    mod.estimate_years("2030-01-01", now=datetime(2026, 1, 1)) == 0.0,
    "未来日期估算为 0（不出现负数）",
)

# ================================================================
section("2. 自动获取商户名称 / 城市 / 照片总数")
REAL = {"9952743": ("富贵面馆(镇坪路店)", "上海", 3379, "2013-05-12"),
        "H3Rq9hMRwjna1wNp": ("名都晓荷塘主题火锅(桦甸店)", "桦甸市", 43, "2019-09-24")}
def isolated_settings(name="probe"):
    """用临时目录里的设置文件，避免读到用户真实设置导致结果不稳定。"""
    folder = Path(tempfile.mkdtemp(prefix=f"dac_set_{name}_"))
    return mod.SettingsManager(path=str(folder / "settings.json"))


http = CountingHTTP(isolated_settings("http"))
html_by_shop = {}
for shop_id in REAL:
    response = http.get_html(f"https://www.dianping.com/shop/{shop_id}/photos")
    html_by_shop[shop_id] = response.text
    meta = mod.parse_shop_meta_from_album(response.text)
    name, city, count, _oldest = REAL[shop_id]
    check(meta["shop_name"] == name, f"{shop_id} 店名 = {name}", f"实际 {meta['shop_name']!r}")
    check(meta["city"] == city, f"{shop_id} 城市 = {city}", f"实际 {meta['city']!r}")
    check(
        meta["photo_count"] == count,
        f"{shop_id} 照片总数 = {count}",
        f"实际 {meta['photo_count']}",
    )
# 带页码的标题（-第N页-）也要能解析
paged = re.sub(r"<title>", "<title>", html_by_shop["9952743"]).replace(
    "<title>富贵面馆(镇坪路店)-图片-上海-大众点评网</title>",
    "<title>富贵面馆(镇坪路店)-图片-上海-第212页-大众点评网</title>",
)
paged_meta = mod.parse_shop_meta_from_album(paged)
check(
    paged_meta["shop_name"] == "富贵面馆(镇坪路店)" and paged_meta["city"] == "上海",
    "带「-第N页-」的标题也能解析出店名/城市",
    f"{paged_meta['shop_name']!r} {paged_meta['city']!r}",
)
# 店名里带 "-" 的情况
dash_meta = mod.parse_shop_meta_from_album(
    "<html><head><title>小杨生煎-南京西路店-图片-上海-大众点评网</title></head></html>"
)
check(
    dash_meta["shop_name"] == "小杨生煎-南京西路店" and dash_meta["city"] == "上海",
    "店名里含「-」也能正确切分",
    f"{dash_meta['shop_name']!r} {dash_meta['city']!r}",
)
check(
    mod.album_last_page(3379, 16) == 212
    and mod.album_last_page(43, 16) == 3
    and mod.album_last_page(16, 16) == 1
    and mod.album_last_page(0, 16) is None,
    "末页 = ceil(总数/每页数)",
)

# ================================================================
section("3. 采集日期范围：真实页面端到端")
work = Path(tempfile.mkdtemp(prefix="dac_feat_"))
try:
    SHOP = "9952743"

    def run_crawl(label, date_from="", date_to="", keep_unknown=True):
        base = work / label
        base.mkdir(parents=True, exist_ok=True)
        settings = mod.SettingsManager(path=str(base / "settings.json"))
        settings.set("date_from", date_from)
        settings.set("date_to", date_to)
        settings.set("date_filter_keep_unknown", keep_unknown)
        settings.set("image_delay", 0)
        settings.set("min_image_dimension", 200)
        db = mod.ArchiveDB(str(base / "archive.db"))
        client = CountingHTTP(settings)
        engine = mod.DianpingArchiveEngine(
            db=db, http_client=client,
            resolver=mod.DianpingURLResolver(client),
            settings=settings, logger=lambda m: None,
        )
        engine.crawl_page(SHOP, 1, base / "out")
        rows = db.conn.execute(
            "SELECT published_at, uploader FROM images ORDER BY published_at"
        ).fetchall()
        page = db.conn.execute(
            "SELECT status, image_count FROM pages WHERE shop_id=? AND page_no=1",
            (SHOP,),
        ).fetchone()
        manifest = base / "out" / "page_1" / "manifest.csv"
        body = []
        if manifest.exists():
            with open(manifest, "r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle)
                next(reader, None)
                body = list(reader)
        shop_row = db.conn.execute(
            "SELECT COALESCE(shop_name,''), COALESCE(city,''), "
            "COALESCE(photo_count,0), COALESCE(oldest_photo_at,'') "
            "FROM shops WHERE shop_id=?", (SHOP,)
        ).fetchone()
        db.close()
        return {
            "rows": rows, "page": page, "manifest": body,
            "shop": shop_row, "images": len(client.image_calls),
        }

    # 3.1 只收 2026-01-31 之前的照片：本页只有 2025-12-18 一张
    result = run_crawl("to_2026_01", date_to="2026-01-31")
    check(
        [row[0] for row in result["rows"]] == ["2025-12-18"],
        "截止日期 2026-01-31 → 只入库 1 张（2025-12-18）",
        f"实际 {[row[0] for row in result['rows']]}",
    )
    skipped = [r for r in result["manifest"] if r[14].startswith("skipped_by_date")]
    check(len(skipped) == 15, "其余 15 张记为 skipped_by_date", f"实际 {len(skipped)}")
    check(result["images"] == 1, "只下载了 1 张图片", f"实际 {result['images']}")
    check(result["page"][0] == "completed", "页面状态 completed", f"{result['page']}")

    # 3.2 只收 2026-08-01 之后的照片
    result = run_crawl("from_2026_08", date_from="2026-08-01")
    dates = [row[0] for row in result["rows"]]
    check(
        dates == ["2026-08-12", "2026-08-16", "2026-09-22"],
        "起始日期 2026-08-01 → 入库 3 张",
        f"实际 {dates}",
    )
    check(result["images"] == 3, "只下载了 3 张图片", f"实际 {result['images']}")

    # 3.3 整页都不在范围内 → 整页跳过
    result = run_crawl("None_2030", date_from="2030-01-01")
    check(not result["rows"], "整页跳过时不入库任何图片")
    check(
        result["page"][0] == "skipped_by_date",
        "页面状态标记为 skipped_by_date",
        f"{result['page']}",
    )
    check(result["images"] == 0, "整页跳过时一次图片请求都不发", f"实际 {result['images']}")
    check(not result["manifest"], "整页跳过时不写 manifest")

    # 3.4 日期未知的图片按开关处理
    result = run_crawl("unknown_keep", date_to="2026-01-31", keep_unknown=True)
    check(len(result["rows"]) == 1, "日期未知且保留时不影响范围判定", f"{len(result['rows'])}")

    # 3.5 采集时自动写入商户信息
    check(
        result["shop"][0] == "富贵面馆(镇坪路店)" and result["shop"][1] == "上海",
        "采集过程中自动写入商户名称/城市",
        f"{result['shop']}",
    )
    check(result["shop"][2] == 3379, "写入相册照片总数", f"{result['shop'][2]}")
    check(result["shop"][3] != "", "写入最早照片日期", f"{result['shop'][3]!r}")

    # ================================================================
    section("4. 末页发现：用内嵌照片总数直接推算（省掉二分探测）")
    base = work / "discovery"
    base.mkdir(parents=True, exist_ok=True)
    settings = mod.SettingsManager(path=str(base / "settings.json"))
    settings.set("image_delay", 0)
    db = mod.ArchiveDB(str(base / "archive.db"))
    client = CountingHTTP(settings)
    engine = mod.DianpingArchiveEngine(
        db=db, http_client=client, resolver=mod.DianpingURLResolver(client),
        settings=settings, logger=lambda m: None,
    )
    last = engine.discover_last_page(SHOP)
    check(last == 212, "末页 = 212", f"实际 {last}")
    check(
        len(client.html_calls) <= 3,
        "发现末页只用了 ≤3 次页面请求",
        f"实际 {len(client.html_calls)} 次: {[u[-28:] for u in client.html_calls]}",
    )
    shop_row = db.conn.execute(
        "SELECT COALESCE(oldest_photo_at,'') FROM shops WHERE shop_id=?", (SHOP,)
    ).fetchone()
    check(
        shop_row and shop_row[0] == "2013-05-12",
        "发现末页时顺手记下最早照片 2013-05-12（用于估算经营年限）",
        f"实际 {shop_row}",
    )
    db.close()

    # ================================================================
    section("5. 自动找店：店铺 ID 提取 / 核验 / 筛选")
    text = """
    分享给你：https://www.dianping.com/shop/9952743/photos?pg=1
    老字号 https://m.dianping.com/shop/2224159
    另一家 /shop/H3Rq9hMRwjna1wNp 和 https://www.dianping.com/shopinfo/102451540
    重复的 https://www.dianping.com/shop/9952743 以及无效链接
    https://www.dianping.com/shop/photos  <a href="/shop/99999999999">x</a>
    """
    ids = mod.extract_shop_ids(text)
    check(
        ids == ["9952743", "2224159", "H3Rq9hMRwjna1wNp", "102451540", "99999999999"],
        "从混合文本里提取店铺 ID（去重、保持顺序、剔除 photos 这类伪 ID）",
        f"实际 {ids}",
    )

    probe_settings = isolated_settings("probe")
    probe_settings.set("discover_delay", 0)
    discovery = mod.ShopDiscovery(
        CountingHTTP(probe_settings), probe_settings,
        db=None, logger=lambda m: None,
    )
    info = discovery.probe_shop("9952743")
    check(info["shop_name"] == "富贵面馆(镇坪路店)", "probe_shop 取到店名", f"{info['shop_name']!r}")
    check(info["city"] == "上海", "probe_shop 取到城市", f"{info['city']!r}")
    check(info["photo_count"] == 3379, "probe_shop 取到照片总数", f"{info['photo_count']}")
    check(
        info["oldest_photo_at"] == "2013-05-12",
        "probe_shop 取到最早照片日期",
        f"{info['oldest_photo_at']!r}",
    )
    check(
        info["estimated_years"] and info["estimated_years"] >= 13,
        "probe_shop 估算出经营年限 ≥13 年",
        f"{info['estimated_years']}",
    )
    bad = discovery.probe_shop("99999999999")
    check(bad["error"] != "", "无效店铺 ID 会被标记为失败", f"{bad['error']!r}")

    ok, reason = discovery.judge(info, city_keyword="上海", min_years=10)
    check(ok, "上海 + 至少 10 年 → 通过", reason)
    ok, reason = discovery.judge(info, city_keyword="北京")
    check(not ok and "城市不符" in reason, "城市不符被过滤", reason)
    ok, reason = discovery.judge(info, min_years=30)
    check(not ok and "最早照片" in reason, "年限不足被过滤并说明最早照片", reason)
    ok, reason = discovery.judge(info, min_photos=99999)
    check(not ok and "照片数" in reason, "照片数不足被过滤", reason)

    # 5.1 完整流程：从「浏览器另存的列表页」找上海 10 年以上的老店
    list_html = (
        "<html><body><ul>"
        '<li><a href="https://www.dianping.com/shop/9952743">富贵面馆</a></li>'
        '<li><a href="https://www.dianping.com/shop/2224159">海底捞</a></li>'
        '<li><a href="https://www.dianping.com/shop/H3Rq9hMRwjna1wNp">桦甸的店</a></li>'
        '<li><a href="https://www.dianping.com/shop/102451540">长沙的店</a></li>'
        '<li><a href="https://www.dianping.com/shop/99999999999">无效</a></li>'
        "</ul></body></html>"
    )
    list_file = work / "上海美食列表页.html"
    list_file.write_text(list_html, encoding="utf-8")

    base = work / "discover"
    base.mkdir(parents=True, exist_ok=True)
    settings = mod.SettingsManager(path=str(base / "settings.json"))
    settings.set("discover_delay", 0)
    db = mod.ArchiveDB(str(base / "archive.db"))
    discovery = mod.ShopDiscovery(
        CountingHTTP(settings), settings, db=db, logger=lambda m: None,
    )
    results, stats = discovery.discover_from_files(
        [str(list_file)], city_keyword="上海", min_years=9, limit=50,
    )
    names = sorted(item["shop_name"] for item in results)
    check(
        names == ["富贵面馆(镇坪路店)", "海底捞火锅(海宁路店)"],
        "从本地列表页找出「上海 + 至少 9 年」的店",
        f"实际 {names}",
    )
    check(stats["total"] == 5 and stats["passed"] == 2, "统计：5 个候选、2 个通过", f"{stats}")

    # 边界：海底捞最早照片是 2016-12-15（约 9.8 年），min_years=10 应该只剩富贵面馆
    discovery2 = mod.ShopDiscovery(
        CountingHTTP(settings), settings, db=None, logger=lambda m: None,
    )
    strict, stats_strict = discovery2.discover_from_files(
        [str(list_file)], city_keyword="上海", min_years=10, limit=50,
    )
    check(
        [item["shop_name"] for item in strict] == ["富贵面馆(镇坪路店)"],
        "边界：至少 10 年时只剩富贵面馆（海底捞约 9.8 年被正确排除）",
        f"实际 {[item['shop_name'] for item in strict]}",
    )
    check(
        any("9.8 年" in key for key in stats_strict["reasons"]),
        "被年限排除时给出估算年限",
        f"{list(stats_strict['reasons'])}",
    )
    check(
        any("城市不符" in key for key in stats["reasons"]),
        "被过滤的原因有记录（城市不符）",
        f"{list(stats['reasons'])}",
    )
    check(
        any("未找到商户信息" in key or "不存在" in key for key in stats["reasons"]),
        "无效店铺 ID 的原因有记录",
        f"{list(stats['reasons'])}",
    )
    candidates = db.get_candidates()
    check(len(candidates) == 2, "结果写入 candidates 表", f"实际 {len(candidates)}")
    check(
        all(row[4] for row in candidates),
        "候选记录里带最早照片日期",
        f"{[row[4] for row in candidates]}",
    )
    db.set_candidate_status("9952743", "collected")
    check(
        [row[7] for row in db.get_candidates(status="collected")] == ["collected"],
        "可以把候选标记为已采集",
    )
    check(db.candidates_stats()["total"] == 2, "候选统计正确")

    # 5.2 从档案库刷新
    db.upsert_shop("9952743", shop_name="旧名字", city="")
    refreshed, stats2 = discovery.discover_from_archive(
        city_keyword="上海", min_years=10,
    )
    row = db.conn.execute(
        "SELECT COALESCE(shop_name,''), COALESCE(city,''), "
        "COALESCE(oldest_photo_at,'') FROM shops WHERE shop_id='9952743'"
    ).fetchone()
    check(
        row[0] == "富贵面馆(镇坪路店)" and row[1] == "上海",
        "档案库刷新会把店名/城市补齐",
        f"{row}",
    )
    check(len(refreshed) == 1, "档案库里符合条件的老店被挑出来", f"{len(refreshed)}")
    db.close()

    # ================================================================
    section("6. 多店铺采集：候选 → 采集队列")
    base = work / "multi"
    base.mkdir(parents=True, exist_ok=True)
    settings = mod.SettingsManager(path=str(base / "settings.json"))
    settings.set("image_delay", 0)
    db = mod.ArchiveDB(str(base / "archive.db"))
    controller = mod.CrawlController(
        db=db, http_client=CountingHTTP(settings),
        resolver=mod.DianpingURLResolver(CountingHTTP(settings)),
        settings=settings, logger=lambda m: None,
    )
    targets = controller._build_targets(
        "", ["9952743", "9952743", "H3Rq9hMRwjna1wNp", ""]
    )
    check(
        [t["shop_id"] for t in targets] == ["9952743", "H3Rq9hMRwjna1wNp"],
        "候选列表去重后变成待采集商户",
        f"{[t['shop_id'] for t in targets]}",
    )
    check(
        all(t.get("from_candidate") for t in targets),
        "候选来源被标记（采集完会写回「已采集」）",
    )
    targets2 = controller._build_targets(
        "https://www.dianping.com/shop/2224159/photos",
        ["2224159", "102451540"],
    )
    check(
        [t["shop_id"] for t in targets2] == ["2224159", "102451540"],
        "输入链接与候选合并时也会去重",
        f"{[t['shop_id'] for t in targets2]}",
    )
    db.close()
finally:
    shutil.rmtree(work, ignore_errors=True)

print(f"\n通过 {len(passed)} 项，失败 {len(failed)} 项")
for item in failed:
    print(f"  FAIL: {item}")
sys.exit(1 if failed else 0)
