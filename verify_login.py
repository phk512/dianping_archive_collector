"""验证新增的「带登录态访问列表页」能力：
- Copy as cURL 解析（bash / cmd 两种）
- 翻页地址构造
- 列表页店铺卡片解析（识别卡片结构 + 兜底扫链接 + 年限提示）
- 风控/cookie 失效的识别；没有 cookie 时的提示
- 真实联网：匿名访问分类页应被识别为风控；相册核验链路不受影响
"""
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

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


# ---------------------------------------------------------------- cURL 解析
section("1. 解析浏览器「Copy as cURL」")
BASH_CURL = r"""curl 'https://www.dianping.com/shanghai/ch10' \
  -H 'accept: text/html,application/xhtml+xml' \
  -H 'cookie: _lxsdk_cuid=18f0abc; cy=8; cye=shanghai; _hc.v=abc-123; t=xyz' \
  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Edg/139.0.0.0' \
  -H 'referer: https://www.dianping.com/' \
  --compressed"""

parsed = mod.parse_curl_command(BASH_CURL)
check(
    parsed["url"] == "https://www.dianping.com/shanghai/ch10",
    "bash 版 cURL：取出网址",
    f"{parsed['url']!r}",
)
check(
    parsed["cookie"].startswith("_lxsdk_cuid=18f0abc")
    and "cye=shanghai" in parsed["cookie"],
    "bash 版 cURL：取出 Cookie",
    f"{parsed['cookie'][:40]}…",
)
check(
    parsed["user_agent"].startswith("Mozilla/5.0") and "Edg/" in parsed["user_agent"],
    "bash 版 cURL：取出 User-Agent",
)
check("referer" in parsed["headers"], "bash 版 cURL：其它请求头也保留")

CMD_CURL = (
    'curl "https://www.dianping.com/shanghai/ch10/p3" ^\n'
    '  -H "cookie: cy=8; cye=shanghai" ^\n'
    '  -H "user-agent: Mozilla/5.0 Edg/139"'
)
cmd_parsed = mod.parse_curl_command(CMD_CURL)
check(
    cmd_parsed["url"] == "https://www.dianping.com/shanghai/ch10/p3",
    "cmd 版 cURL（^ 续行 + 双引号）也能解析",
    f"{cmd_parsed['url']!r}",
)
check(cmd_parsed["cookie"] == "cy=8; cye=shanghai", "cmd 版 cURL：Cookie 正确")
check(
    cmd_parsed["user_agent"] == "Mozilla/5.0 Edg/139", "cmd 版 cURL：UA 正确"
)
# cmd 里偶尔会把引号写成 ^" 的形式，也要能认
caret_parsed = mod.parse_curl_command(
    'curl ^"https://www.dianping.com/shanghai/ch10^" ^\n'
    '  -H ^"cookie: cy=8^"'
)
check(
    caret_parsed["url"] == "https://www.dianping.com/shanghai/ch10"
    and caret_parsed["cookie"] == "cy=8",
    'cmd 的 ^" 转义写法也能解析',
    f"{caret_parsed}",
)

check(
    mod.parse_curl_command("").get("url", "") == ""
    and mod.parse_curl_command("随便一段文字").get("cookie", "") == "",
    "非 cURL 文本不会误解析",
)

# ---------------------------------------------------------------- 翻页地址
section("2. 列表页翻页地址构造")
cases = [
    ("https://www.dianping.com/shanghai/ch10", 1, "https://www.dianping.com/shanghai/ch10"),
    ("https://www.dianping.com/shanghai/ch10", 2, "https://www.dianping.com/shanghai/ch10/p2"),
    ("https://www.dianping.com/shanghai/ch10/p5", 3, "https://www.dianping.com/shanghai/ch10/p3"),
    ("https://www.dianping.com/shanghai/ch10/p5", 1, "https://www.dianping.com/shanghai/ch10"),
    ("https://www.dianping.com/shop/9952743/photos?pg=2", 4,
     "https://www.dianping.com/shop/9952743/photos?pg=4"),
    ("https://www.dianping.com/search/keyword/1/0_%E9%9D%A2%E9%A6%86", 2,
     "https://www.dianping.com/search/keyword/1/0_%E9%9D%A2%E9%A6%86/p2"),
    ("https://www.dianping.com/shanghai/ch10?a=1", 2,
     "https://www.dianping.com/shanghai/ch10/p2?a=1"),
    ("", 2, ""),
]
for base, page, expected in cases:
    got = mod.list_page_url(base, page)
    check(got == expected, f"list_page_url({base[-28:] or '空'}, {page})", f"实际 {got}")

# ---------------------------------------------------------------- 列表页解析
section("3. 列表页店铺卡片解析")
# 结构 1：点评常见的 #shop-all-list（带名称、评分、年限标注）
LIST_HTML_1 = """
<html><body>
<div id="shop-all-list">
  <ul>
    <li>
      <div class="txt">
        <div class="tit"><a href="/shop/9952743" onclick="...">
          <h4>富贵面馆(镇坪路店)</h4></a></div>
        <div class="comment"><span class="score">4.6</span> 1288 条点评</div>
        <div class="tag">15年老店 本帮面</div>
      </div>
    </li>
    <li>
      <div class="txt">
        <div class="tit"><a href="/shop/2224159"><h4>海底捞火锅(海宁路店)</h4></a></div>
        <div class="tag">老字号 火锅</div>
      </div>
    </li>
  </ul>
</div>
<div class="footer"><a href="/shop/photos">图片</a><a href="/about">关于</a>
<a href="https://www.dianping.com/shanghai/ch10/p2">下一页</a></div>
</body></html>
"""
def isolated_settings(name="probe"):
    folder = Path(tempfile.mkdtemp(prefix=f"dac_login_{name}_"))
    return mod.SettingsManager(path=str(folder / "settings.json"))


discovery = mod.ShopDiscovery(
    mod.HTTPClient(isolated_settings("a")), isolated_settings("b"),
    db=None, logger=lambda m: None,
)
cards = discovery.shop_cards_from_list(LIST_HTML_1)
check(len(cards) == 2, "识别 #shop-all-list 里的 2 家店铺", f"{cards}")
check(cards[0][0] == "9952743" and "富贵面馆" in cards[0][1], "取出店铺 ID 与名称", f"{cards[0]}")
check("15 年老店" in cards[0][2], "识别「15年老店」页面标注", f"{cards[0][2]!r}")
check("老字号" in cards[1][2], "识别「老字号」页面标注", f"{cards[1][2]!r}")
check(
    all(card[0] not in ("photos", "about") for card in cards),
    "杂项链接（/shop/photos 等）不会被当成店铺",
)

# 结构 2：完全没有已知卡片结构 -> 兜底扫 /shop/ 链接
LIST_HTML_2 = """
<html><body><div class="unknown-layout">
 <a href="https://www.dianping.com/shop/k7SXDWs4mWWlTa7Z">马记米皮</a>
 <a href="/shopshare/102451540">某店</a>
 <a href="https://www.dianping.com/shop/9952743/photos">富贵面馆图片</a>
 <a href="https://www.dianping.com/shanghai/ch10/p2">下一页</a>
</div></body></html>
"""
cards2 = discovery.shop_cards_from_list(LIST_HTML_2)
ids2 = [card[0] for card in cards2]
check(
    ids2 == ["k7SXDWs4mWWlTa7Z", "102451540", "9952743"],
    "未知版式兜底：仍然能拿全店铺 ID（含 shopshare 与 /photos 形式）",
    f"{ids2}",
)

# ---------------------------------------------------------------- 登录态
section("4. 登录态与风控识别")
settings = isolated_settings("s")
client = mod.HTTPClient(settings)
check(not client.has_cookie, "默认没有 Cookie")
settings.set("cookie", "_lxsdk_cuid=1; cy=8; cye=shanghai")
settings.set("user_agent", "Mozilla/5.0 (Windows NT 10.0) Edg/139.0.0.0")
client.apply_login_state()
check(client.has_cookie, "写入设置后会话带上 Cookie")
check("Edg/" in client.session.headers["User-Agent"], "同时覆盖 User-Agent")
settings.set("cookie", "")
settings.set("user_agent", "")
client.apply_login_state()
check(
    not client.has_cookie
    and client.session.headers["User-Agent"] == mod.USER_AGENT,
    "清空后恢复默认（不会残留旧登录态）",
    f"{client.session.headers['User-Agent'][:40]}",
)
settings.set("cookie", "cy=8")
settings.set("user_agent", "Custom UA")
client.apply_login_state()
check(
    client.session.headers["Cookie"] == "cy=8"
    and client.session.headers["User-Agent"] == "Custom UA",
    "重新应用设置即时生效（不用重启）",
)

check(
    mod.is_verify_page("https://verify.meituan.com/v2/app/general_page?x=1")
    and mod.is_verify_page("<title>验证中心</title>")
    and mod.is_verify_page('action=spiderindefence')
    and not mod.is_verify_page("https://www.dianping.com/shanghai/ch10"),
    "风控页识别正确",
)

# ---------------------------------------------------------------- 真实联网
section("5. 真实联网：没有 Cookie 时能给出可操作的提示")
settings = isolated_settings("s")
settings.set("discover_delay", 0)
settings.set("list_url", "https://www.dianping.com/shanghai/ch10")
http = mod.HTTPClient(settings)
discovery = mod.ShopDiscovery(http, settings, db=None, logger=lambda m: None)

ok, message = discovery.test_list_url("https://www.dianping.com/shanghai/ch10")
check(not ok, "匿名测试列表页应失败", f"{message[:60]}")
check("Cookie" in message or "风控" in message, "失败提示里说明了原因与办法", f"{message[:80]}")

html, error = discovery.fetch_list_page("https://www.dianping.com/shanghai/ch10")
check(error != "" and "风控" in error, "fetch_list_page 明确报出被风控", f"{error[:60]}")

results, stats = discovery.discover_from_url(
    "https://www.dianping.com/shanghai/ch10", max_pages=1, limit=5,
)
check(
    results == [] and stats.get("error"),
    "discover_from_url 遇到风控会安全中止并给出原因",
    f"{stats.get('error', '')[:60]}",
)

# 相册核验链路不受登录态影响（列表页被拦，但相册页照常）
info = discovery.probe_shop("9952743")
check(
    info["error"] == "" and info["shop_name"] == "富贵面馆(镇坪路店)",
    "没有 Cookie 时相册核验仍然正常（不受影响）",
    f"{info['shop_name']!r} / {info['estimated_years']} 年",
)

print(f"\n通过 {len(passed)} 项，失败 {len(failed)} 项")
for item in failed:
    print(f"  FAIL: {item}")
sys.exit(1 if failed else 0)
