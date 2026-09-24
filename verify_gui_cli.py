"""验证新功能在 CLI 与 GUI 里的接入。"""
import importlib.util
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "dianping_archive_collector_v3_2.py"
spec = importlib.util.spec_from_file_location("dianping_v3", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules["dianping_v3"] = mod
spec.loader.exec_module(mod)

failed = []


def check(condition, label, detail=""):
    if not condition:
        failed.append(label)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


work = Path(tempfile.mkdtemp(prefix="dac_cg_"))
os.chdir(work)  # 界面默认把档案目录建在当前目录，切到临时目录避免污染
try:
    print("\n--- CLI: --help / --check-deps / --selftest ---")
    check(mod.main(["--help"]) == 0, "--help 返回 0")
    check(mod.main(["--check-deps"]) == 0, "--check-deps 返回 0")
    check(mod.main(["--selftest"]) == 0, "--selftest 返回 0")

    print("\n--- CLI: --parse-html ---")
    page = work / "page.html"
    request = urllib.request.Request(
        "https://www.dianping.com/shop/9952743/photos?pg=1",
        headers={"User-Agent": mod.USER_AGENT, "Referer": "https://www.dianping.com/"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        page.write_bytes(response.read())
    check(mod.cli_parse_html(str(page)) == 0, "--parse-html 返回 0")

    print("\n--- CLI: --probe ---")
    check(mod.cli_probe(["9952743"]) == 0, "--probe 返回 0")
    check(mod.cli_probe([]) == 2, "--probe 无参数返回 2")

    print("\n--- CLI: --discover ---")
    list_file = work / "list.html"
    list_file.write_text(
        '<html><body>'
        '<a href="https://www.dianping.com/shop/9952743">富贵面馆</a>'
        '<a href="https://www.dianping.com/shop/H3Rq9hMRwjna1wNp">桦甸的店</a>'
        "</body></html>",
        encoding="utf-8",
    )
    check(
        mod.cli_discover([str(list_file)], city_keyword="上海", min_years=9) == 0,
        "--discover 返回 0",
    )
    check(mod.cli_discover([]) == 2, "--discover 无参数返回 2")
    check(
        mod.main(["--discover", str(list_file), "--city", "上海",
                  "--min-years", "9", "--limit", "10"]) == 0,
        "main() 解析 --discover 的参数",
    )

    print("\n--- CLI: --discover-url ---")
    check(mod.cli_discover_url("") == 2, "--discover-url 无网址返回 2")
    check(mod.main(["--discover-url"]) == 2, "main() 对缺少网址返回 2")
    check(
        mod.main(["--discover-url", "https://www.dianping.com/shanghai/ch10",
                  "--max-pages", "1", "--city", "上海", "--min-years", "9",
                  "--limit", "3"]) == 0,
        "main() 解析 --discover-url 的参数（没有 Cookie 时应安全中止并说明原因）",
    )
    curl_file = work / "copy_as_curl.txt"
    curl_file.write_text(
        "curl 'https://www.dianping.com/shanghai/ch10' \\\n"
        "  -H 'cookie: cy=8; cye=shanghai; _lxsdk_cuid=1' \\\n"
        "  -H 'user-agent: Mozilla/5.0 Edg/139'",
        encoding="utf-8",
    )
    check(
        mod.cli_discover_url(
            "https://www.dianping.com/shanghai/ch10",
            curl_file=str(curl_file), max_pages=1, limit=3,
        ) == 0,
        "--discover-url --curl 能读入登录态并正常结束",
    )

    print("\n--- GUI：新标签页与候选表 ---")
    import tkinter as tk

    try:
        root = tk.Tk()
    except Exception as exc:
        print(f"  跳过：无法创建 Tk 窗口（{type(exc).__name__}: {exc}）")
    else:
        root.withdraw()
        settings = mod.SettingsManager(path=str(work / "settings.json"))
        app = mod.DianpingArchiveGUI(root, settings)

        check(hasattr(app, "discover_tab"), "存在「自动找店」标签页")
        check(hasattr(app, "candidate_tree"), "候选表格已创建")

        # 设置页包含新字段
        for key in ("date_from", "date_to", "date_filter_keep_unknown",
                    "discover_city_keyword", "discover_min_years",
                    "discover_min_photos", "discover_limit", "discover_delay"):
            check(key in app.settings_ui.vars, f"设置页包含 {key}")

        # 保存设置：日期被规整
        app.settings_ui.vars["date_from"][1].set("2010")
        app.settings_ui.vars["date_to"][1].set("2012-06")
        app.settings_ui.vars["discover_min_years"][1].set("15")
        app.settings_ui.save_settings()
        check(
            app.settings.get("date_from") == "2010-01-01"
            and app.settings.get("date_to") == "2012-06-01",
            "保存设置时日期被规整成 YYYY-MM-DD",
            f"{app.settings.get('date_from')} / {app.settings.get('date_to')}",
        )
        check(app.settings.get("discover_min_years") == 15, "找店年限被保存")

        # 候选表渲染 + 档案树显示经营年限
        app.db.upsert_candidate(
            shop_id="9952743", shop_name="富贵面馆(镇坪路店)", city="上海",
            photo_count=3379, oldest_photo_at="2013-05-12",
            estimated_years=13.4, source="测试", status="candidate",
        )
        app.db.upsert_shop(
            "9952743", shop_name="富贵面馆(镇坪路店)", city="上海",
            photo_count=3379, oldest_photo_at="2013-05-12",
        )
        count = app.refresh_candidates()
        check(count == 1, "候选表载入 1 条", f"实际 {count}")
        values = app.candidate_tree.item("9952743", "values")
        check(
            values[0] == "富贵面馆(镇坪路店)" and "13" in str(values[4]),
            "候选行显示店名与「至少经营」",
            f"{values}",
        )

        # 列表页网址 / 登录态面板
        check(hasattr(app, "discover_url"), "找店页有「列表页网址」输入框")
        check(hasattr(app, "cookie_status_label"), "找店页有登录态状态标签")
        check(
            "未配置" in app.cookie_status_label.cget("text"),
            "初始状态显示未配置登录态",
            f"{app.cookie_status_label.cget('text')!r}",
        )

        # 粘贴 cURL -> 解析出 Cookie / 网址（把弹窗换成记录，避免测试卡住）
        popups = []
        original_info = mod.messagebox.showinfo
        original_warn = mod.messagebox.showwarning
        original_yesno = mod.messagebox.askyesno
        mod.messagebox.showinfo = lambda *a, **k: popups.append(("info", a))
        mod.messagebox.showwarning = lambda *a, **k: popups.append(("warn", a))
        mod.messagebox.askyesno = lambda *a, **k: True
        try:
            app.discover_cookie_text.insert(
                "1.0",
                "curl 'https://www.dianping.com/shanghai/ch10' -H "
                "'cookie: cy=8; cye=shanghai; _lxsdk_cuid=1' -H "
                "'user-agent: Mozilla/5.0 Edg/139'",
            )
            app.parse_cookie_input()
            check(
                app.settings.get("cookie") == "cy=8; cye=shanghai; _lxsdk_cuid=1",
                "「解析 cURL」把 Cookie 存进设置",
                f"{app.settings.get('cookie')!r}",
            )
            check(
                app.settings.get("user_agent").startswith("Mozilla/5.0 Edg/"),
                "同时存下 User-Agent",
            )
            check(
                app.discover_url.get() == "https://www.dianping.com/shanghai/ch10",
                "自动填入列表页网址",
                f"{app.discover_url.get()!r}",
            )
            check(
                "已配置" in app.cookie_status_label.cget("text"),
                "状态标签更新为已配置",
                f"{app.cookie_status_label.cget('text')!r}",
            )
            check(
                app.http.session.headers.get("Cookie") == app.settings.get("cookie"),
                "HTTP 会话立即带上登录态",
            )
            check(bool(popups), "解析成功后给了提示", f"{popups[:1]}")

            # 清除登录态
            app.clear_cookie_input()
            check(
                app.settings.get("cookie") == "" and not app.http.has_cookie,
                "「清除登录态」会清空设置与会话",
                f"{app.settings.get('cookie')!r}",
            )
            check(
                "未配置" in app.cookie_status_label.cget("text"),
                "清除后状态标签回到未配置",
                f"{app.cookie_status_label.cget('text')!r}",
            )
        finally:
            mod.messagebox.showinfo = original_info
            mod.messagebox.showwarning = original_warn
            mod.messagebox.askyesno = original_yesno
        app.browser.refresh_shops()
        tree_texts = [
            app.browser.shop_tree.item(node, "text")
            for parent in app.browser.shop_tree.get_children()
            for node in app.browser.shop_tree.get_children(parent)
        ]
        check(
            any("至少13年" in text for text in tree_texts),
            "档案库里显示「至少N年」",
            f"{tree_texts}",
        )
        app.browser.on_shop_select.__self__  # noqa: B018  (存在即可)
        app.browser.destroy()
        app.db.close()
        root.destroy()
finally:
    shutil.rmtree(work, ignore_errors=True)

print(f"\n失败 {len(failed)} 项")
for item in failed:
    print(f"  FAIL: {item}")
sys.exit(1 if failed else 0)
