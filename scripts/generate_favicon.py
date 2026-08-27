#!/usr/bin/env python3
"""按 favicon.svg 的几何生成位图图标，保证矢量与位图版本视觉一致。

存在这个脚本而不是手工导出，是为了让位图图标可复现：改了 favicon.svg 之后
重跑一次即可，不必凭记忆重算坐标。生成结果需要提交进仓库——运行环境里没有
SVG 光栅化库，构建期无法即时生成。

用法：
    ./venv/bin/python scripts/generate_favicon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT_DIR / "src" / "server" / "static" / "images"
# 与 favicon.svg 的 viewBox 一致，所有坐标按 32 为基准再等比放大
VIEWBOX = 32
# 超采样倍数：Pillow 的绘图不做抗锯齿，放大绘制再缩小是常规做法
SUPERSAMPLE = 8
BACKGROUND = (31, 38, 50, 255)
FRAME = (245, 247, 250, 255)
SUN = (245, 183, 78, 255)
MOUNTAIN = (90, 169, 248, 255)
# ICO 内含多尺寸，浏览器与操作系统各取所需
ICO_SIZES = (16, 32, 48, 64)
APPLE_TOUCH_SIZE = 180


def render(size: int) -> Image.Image:
    """按指定边长渲染一张方形图标。

    Args:
        size: 输出边长像素。

    Returns:
        RGBA 模式的方形图标。
    """
    scale = size * SUPERSAMPLE / VIEWBOX
    canvas = Image.new("RGBA", (size * SUPERSAMPLE, size * SUPERSAMPLE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    def point(x: float, y: float) -> tuple[float, float]:
        """把 viewBox 坐标换算为画布像素坐标。"""
        return (x * scale, y * scale)

    def box(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
        """把 viewBox 矩形换算为画布像素矩形。"""
        return [point(x0, y0), point(x1, y1)]

    draw.rounded_rectangle(box(0, 0, VIEWBOX, VIEWBOX), radius=7 * scale, fill=BACKGROUND)
    draw.rounded_rectangle(
        box(6.5, 7.5, 25.5, 24.5),
        radius=2.5 * scale,
        outline=FRAME,
        width=max(1, round(2 * scale)),
    )
    draw.ellipse(box(10.5, 11, 14.5, 15), fill=SUN)
    draw.line(
        [
            point(7.5, 21.5),
            point(13, 16),
            point(17, 20),
            point(20.5, 16.5),
            point(24.5, 21.5),
        ],
        fill=MOUNTAIN,
        width=max(1, round(2 * scale)),
        joint="curve",
    )
    return canvas.resize((size, size), Image.LANCZOS)


def main() -> int:
    """生成 ICO 与 apple-touch-icon 两个位图产物。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    largest = render(max(ICO_SIZES))
    ico_path = OUTPUT_DIR / "favicon.ico"
    largest.save(ico_path, format="ICO", sizes=[(size, size) for size in ICO_SIZES])

    apple_path = OUTPUT_DIR / "apple-touch-icon.png"
    # 添加到主屏幕时 iOS 不做圆角裁切以外的处理，直接给它一张实心底的方图
    render(APPLE_TOUCH_SIZE).save(apple_path, format="PNG", optimize=True)

    for path in (ico_path, apple_path):
        print(f"{path.relative_to(ROOT_DIR)} {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
