#!/usr/bin/env python3
"""把新版预览 PNG 转成「SVG 内嵌 PNG」，这样纯文本也能推送到 GitHub。

用法: .venv/bin/python scripts/make_preview_svg.py
生成: preview/<名字>.svg，尺寸压到最长边 1680
"""
import base64
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREVIEW = os.path.join(ROOT, "preview")
MAX_EDGE = 1680

# 新版界面要进仓库的截图
NAMES = [
    "dashboard",
    "tab-abnormal",
    "rooms",
    "recognize",
    "account-detail",
    "account-form",
    "settings-vision",
    "export",
]


def png_size(path):
    with open(path, "rb") as handle:
        head = handle.read(24)
    return struct.unpack(">II", head[16:24])


def build(name):
    src = os.path.join(PREVIEW, f"{name}.png")
    if not os.path.exists(src):
        print(f"跳过 {name}.png（不存在）")
        return None
    resized = os.path.join("/tmp", f"prev_{name}.png")
    subprocess.run(
        ["sips", "-Z", str(MAX_EDGE), src, "--out", resized],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    width, height = png_size(resized)
    data = base64.b64encode(open(resized, "rb").read()).decode("ascii")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        f'<image width="{width}" height="{height}" href="data:image/png;base64,{data}"/>\n'
        '</svg>\n'
    )
    dst = os.path.join(PREVIEW, f"{name}.svg")
    with open(dst, "w", encoding="utf-8") as handle:
        handle.write(svg)
    print(f"✅ {name}.svg  {width}x{height}  {os.path.getsize(dst)//1024} KB")
    return dst


def main():
    if not os.path.isdir(PREVIEW):
        print("找不到 preview 目录")
        return 1
    for name in NAMES:
        build(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
