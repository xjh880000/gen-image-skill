# -*- coding: utf-8 -*-
r"""把一整个图片目录打包成**自包含 HTML**（图片 base64 内联，打开就是图）。

为什么需要它（2026-10-06 实测）：
  · DSH 桌面版（E:\dsh desktop\DeepSeek Harness.exe）的 app.asar 里没打 Markdown 补丁
    → 正文 `![]()` 引本地图可能不显示；
  · `/api/file` 需要鉴权（不带凭据直连返回 401 unauthorized）→ `<img>` 带不上凭据就是裂图；
  · HTML 预览里写相对路径必裂（DSH 预览 iframe 的基址不是文件目录）。
  → 把图片 base64 直接写进 HTML，上面三条**全部绕开**：不依赖路径、不依赖接口、不需要鉴权。

用法：
  python make_selfcontained.py --dir "D:\out\台灯布局" --out "D:\out\图集.html" --title "台灯布局对比"
  python make_selfcontained.py --dir "D:\out" --out "D:\out\图集.html" --captions "D:\out\caps.json"
  python make_selfcontained.py --dir "D:\out" --out "D:\out\图集.html" --width 1200 --quality 88

captions JSON 形状（key 是**文件名前缀**，按最长前缀匹配；不传就用文件名当标题）：
  {"01_步骤": ["第 0 步 · 配件总览", "主板、CPU、内存…先全部摊开点一遍"],
   "02_步骤": ["第 1 步 · 主板出袋", "垫在包装盒上，别直接放桌面"]}

实测效果：Pillow 缩到宽 900 / JPEG q82，16 张图 → 图片合计 1.38 MB → HTML 1.84 MB，秒开。

实现约束：HTML 模板一律 `{{占位符}}.replace()`，**禁止用 `%` 格式化** —— CSS 里的 `100%` 会直接炸。
"""
from __future__ import annotations

import argparse
import base64
import html as _html
import io
import json
import os
import re
import sys
import time

EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}}</title>
<style>
 :root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e8eaf0;--fg2:#9aa3b2;--fg3:#6b7484;--hi:#3ecf8e}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
 header{padding:16px 22px 12px;border-bottom:1px solid var(--line);position:sticky;top:0;background:rgba(15,17,21,.94);backdrop-filter:blur(8px);z-index:9}
 h1{margin:0 0 4px;font-size:16px}
 .sub{color:var(--fg3);font-size:12.5px}
 .wrap{padding:14px 22px 40px;display:grid;gap:14px}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
 .card img{display:block;width:100%;height:auto;background:#0b0d11;cursor:zoom-in}
 .meta{padding:10px 13px 12px}
 .t{font-size:14px;font-weight:600;color:var(--hi);margin-bottom:3px}
 .d{font-size:12.5px;color:var(--fg2)}
 .f{font-size:10.5px;color:var(--fg3);font-family:ui-monospace,Consolas,monospace;margin-top:5px}
 .lb{position:fixed;inset:0;background:rgba(0,0,0,.94);display:none;align-items:center;justify-content:center;z-index:99;cursor:zoom-out;padding:20px}
 .lb.on{display:flex}.lb img{max-width:100%;max-height:100%;border-radius:8px}
</style></head><body>
<header><h1>{{TITLE}}</h1>
<div class="sub">共 <b>{{N}}</b> 张 &nbsp;·&nbsp; 图片已 base64 内联进本文件（不依赖任何路径/接口/鉴权）&nbsp;·&nbsp; 点图放大，ESC 关闭</div></header>
<div class="wrap">{{CARDS}}</div>
<div class="lb" id="lb"><img id="lbi" alt=""></div>
<script>
 document.querySelectorAll('.card img').forEach(function(im){
   im.addEventListener('click',function(){document.getElementById('lbi').src=im.src;document.getElementById('lb').classList.add('on')});
 });
 document.getElementById('lb').addEventListener('click',function(){this.classList.remove('on')});
 document.addEventListener('keydown',function(e){if(e.key==='Escape')document.getElementById('lb').classList.remove('on')});
</script></body></html>
"""

CARD = r"""<div class="card"><img src="data:{{MIME}};base64,{{B64}}" alt="{{T}}">
<div class="meta"><div class="t">{{T}}</div><div class="d">{{D}}</div><div class="f">{{F}}</div></div></div>"""


def natural_key(name: str):
    """让 2 排在 10 前面（纯字典序会 10 < 2）。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def collect_images(src_dir: str):
    out = []
    for n in os.listdir(src_dir):
        p = os.path.join(src_dir, n)
        if os.path.isfile(p) and n.lower().endswith(EXTS):
            out.append(n)
    out.sort(key=natural_key)
    return out


def load_captions(path):
    """{"前缀": ["标题","说明"]} / {"前缀": "标题"} 都吃。"""
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('captions 必须是 JSON 对象：{"文件名前缀": ["标题","说明"]}')
    caps = {}
    for k, v in data.items():
        if isinstance(v, (list, tuple)):
            t = str(v[0]) if len(v) > 0 else ""
            d = str(v[1]) if len(v) > 1 else ""
        else:
            t, d = str(v), ""
        caps[str(k)] = (t, d)
    return caps


def pick_caption(name: str, caps: dict):
    """按**最长**文件名前缀匹配（避免 "01" 抢走 "01_步骤" 的说明）。"""
    best = None
    for k in caps:
        if name.startswith(k) and (best is None or len(k) > len(best)):
            best = k
    return caps[best] if best else None


def to_inline(path: str, width: int, quality: int, has_pil: bool):
    """返回 (bytes, mime)。有 Pillow 就缩放+转 JPEG，没有就原样读。"""
    if not has_pil:
        with open(path, "rb") as f:
            data = f.read()
        return data, MIME.get(os.path.splitext(path)[1].lower(), "image/png")

    from PIL import Image
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
    im = Image.open(path)
    im.load()
    # 透明图必须先垫白底 —— 直接 convert("RGB") 会把透明区变成黑块
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    if width and im.width > width:
        im = im.resize((width, max(1, round(im.height * width / im.width))), resample)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue(), "image/jpeg"


def build(src_dir, out_path, title=None, captions_path=None, width=900, quality=82):
    t0 = time.time()
    src_dir = os.path.abspath(src_dir)
    if not os.path.isdir(src_dir):
        print("❌ 不是目录：%s" % src_dir)
        return 1

    files = collect_images(src_dir)
    if not files:
        print("❌ %s 下没找到图片（支持 %s）" % (src_dir, " / ".join(EXTS)))
        return 1

    try:
        caps = load_captions(captions_path)
    except Exception as e:
        print("❌ captions 读取失败：%s" % e)
        return 1

    try:
        from PIL import Image  # noqa: F401
        has_pil = True
    except Exception:
        has_pil = False
        print("⚠️ 没有 Pillow：将**直接内联原文件**（HTML 会大很多，且不做缩放）")
        print("   装上就好：pip install Pillow")

    cards, failed = [], []
    raw_total = 0
    for name in files:
        p = os.path.join(src_dir, name)
        stem = os.path.splitext(name)[0]
        cap = pick_caption(name, caps)
        t, d = cap if cap else (stem, "")
        try:
            data, mime = to_inline(p, width, quality, has_pil)
        except Exception as e:
            failed.append((name, str(e)))
            print("⚠️ 跳过 %s：%s" % (name, e))
            continue
        raw_total += len(data)
        cards.append(
            CARD.replace("{{MIME}}", mime)
                .replace("{{B64}}", base64.b64encode(data).decode("ascii"))
                .replace("{{T}}", _html.escape(t or stem))
                .replace("{{D}}", _html.escape(d))
                .replace("{{F}}", _html.escape("%s · %.0f KB" % (name, len(data) / 1024.0)))
        )

    if not cards:
        print("❌ 一张都没能内联，HTML 未生成")
        return 1

    page = (PAGE.replace("{{TITLE}}", _html.escape(title or os.path.basename(src_dir)))
                .replace("{{N}}", str(len(cards)))
                .replace("{{CARDS}}", "\n".join(cards)))

    parent = os.path.dirname(os.path.abspath(out_path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    # 一律 UTF-8 **无 BOM**（有 BOM 会让某些解析器把首字符读成乱码）
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)

    size = os.path.getsize(out_path)
    print("✅ 内联 %d/%d 张 · 图片合计 %.2f MB → HTML %.2f MB（%.1fs）"
          % (len(cards), len(files), raw_total / 1048576.0, size / 1048576.0, time.time() - t0))
    print("   %s" % os.path.abspath(out_path))
    if failed:
        print("⚠️ %d 张失败：%s" % (len(failed), "、".join(n for n, _ in failed)))
    if size > 20 * 1048576:
        print("⚠️ HTML 超过 20 MB —— 用 Start-Process 本地打开没问题，"
              "但想在 DSH 里预览会被 /api/file 的 20MB 上限挡掉（返回 413），"
              "可调小 --width / --quality")
    return 1 if failed else 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="把图片目录打包成自包含 HTML（图片 base64 内联，打开就是图）")
    ap.add_argument("--dir", required=True, help="图片目录（按文件名自然排序）")
    ap.add_argument("--out", required=True, help="输出的 .html 路径")
    ap.add_argument("--title", default=None, help="页面标题（默认用目录名）")
    ap.add_argument("--captions", default=None,
                    help='说明 JSON：{"文件名前缀": ["标题","说明"]}，最长前缀匹配；不传就用文件名当标题')
    ap.add_argument("--width", type=int, default=900, help="超过这个宽度就等比缩小（默认 900）")
    ap.add_argument("--quality", type=int, default=82, help="JPEG 质量 1~95（默认 82）")
    a = ap.parse_args(argv)
    return build(a.dir, a.out, a.title, a.captions, a.width, a.quality)


if __name__ == "__main__":
    raise SystemExit(main())
