"""抓取控制台的真实运行截图，供 README 与作品集使用。

为什么用脚本而不是手动截图
--------------------------
截图是最容易过期的东西：改一行前端样式，README 里的图就变成"和实际不符"。
把抓图固化成脚本，改完前端重跑一次即可，也保证图里显示的是**真实跑出来的结果**
（脚本会真的提问并等回答，不会摆一个假数据）。

用法：先把服务起在 8021，然后

    python scripts/shots.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
BASE = os.environ.get("IP_SHOT_BASE", "http://127.0.0.1:8021")

# 截图前把对话区滚回顶部：结论在卡片最上面，滚到底只能看到表格尾巴。
SCROLL_TOP = "document.querySelector('.chat-scroll').scrollTop = 0"

QUESTIONS = [
    ("各分类的书籍数量", "01-overview"),
    ("评分人数最多的前10本书是哪些？", "02-ranking"),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1500, "height": 940},
                                device_scale_factor=2)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "00-landing.png"))

        for question, name in QUESTIONS:
            page.fill("#question", question)
            page.click("#send")
            # 等结论卡片出现。SSE 是流式的，只能等一个明确的可视标志；
            # 排除 card-pending，否则会在"正在执行"那一刻就截图。
            page.wait_for_selector(".msg-bot .card:not(.card-pending)", timeout=90000)
            page.wait_for_timeout(2500)
            page.evaluate(SCROLL_TOP)
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / f"{name}.png"))
            page.wait_for_timeout(500)

        browser.close()
    for path in sorted(OUT.glob("*.png")):
        print(f"{path.name}  {path.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
