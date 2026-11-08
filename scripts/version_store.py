#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
version_store.py —— 图片迭代的「版本存储」与「本地调色」

职责边界（刻意不碰网络）：
  · 版本存储：扫描/分配版本号、写 _manifest.json、写 当前版本.txt
  · 本地调色：纯 PIL 的亮度/对比度/饱和度/伽马调整，零 API 调用、零画质劣化

设计依据（2026-09-23 调研）：
  · 命名 `{主题}_v{N}_{模式}_v{基于几}`，其中「基于几」写的是**从哪张来的**，
    不是「上一张」——这样重新生成造成的谱系分叉在文件名排序里就能看出来
  · 模式四选一：gen（首次）/ edit（渐进编辑）/ regen（重新生成）/ adjust（本地调色）
  · 旧版本永不删除（一张 1~2MB，20 轮也才 40MB，但删了就没法回退）
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

MODES = ("gen", "edit", "regen", "adjust")

MANIFEST_NAME = "_manifest.json"
CURRENT_NAME = "当前版本.txt"

# 文件名形如：台灯布局_v4_regen_v1.png  /  台灯布局_v1_gen.png
FNAME_RE = re.compile(r"^(?P<theme>.+?)_v(?P<ver>\d+)_(?P<mode>gen|edit|regen|adjust)(?:_v(?P<base>\d+))?\.(?P<ext>png|jpg|jpeg|webp)$",
                      re.IGNORECASE)


# ---------------------------------------------------------------- 版本存储

def parse_versions(out_dir: str, theme: str) -> list[dict]:
    """扫描目录，返回该主题下所有版本（按版本号升序）。"""
    out = []
    if not os.path.isdir(out_dir):
        return out
    for name in os.listdir(out_dir):
        m = FNAME_RE.match(name)
        if not m:
            continue
        if m.group("theme") != theme:
            continue
        out.append({
            "version": int(m.group("ver")),
            "mode": m.group("mode").lower(),
            "base": int(m.group("base")) if m.group("base") else None,
            "file": name,
            "path": os.path.abspath(os.path.join(out_dir, name)),
        })
    out.sort(key=lambda x: x["version"])
    return out


def read_manifest(out_dir: str) -> dict:
    p = os.path.join(out_dir, MANIFEST_NAME)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # manifest 坏了不能让主流程挂掉：退回空表，后面重建
        return {}


def write_manifest(out_dir: str, theme: str, data: dict) -> str:
    p = os.path.join(out_dir, MANIFEST_NAME)
    os.makedirs(out_dir, exist_ok=True)
    all_data = read_manifest(out_dir)
    all_data[theme] = data
    with open(p, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    return p


def write_current(out_dir: str, filename: str) -> str:
    p = os.path.join(out_dir, CURRENT_NAME)
    os.makedirs(out_dir, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(filename)
    return p


def next_version(out_dir: str, theme: str) -> int:
    vs = parse_versions(out_dir, theme)
    return (max(v["version"] for v in vs) + 1) if vs else 1


def version_of_file(path: str) -> int | None:
    """从文件名反解出版本号。用于「这一版到底基于哪张」——必须看真实源图，
    不能拿「当前版本」顶替（否则谱系会记错，回退就回错了）。"""
    m = FNAME_RE.match(os.path.basename(path))
    return int(m.group("ver")) if m else None


def make_filename(theme: str, version: int, mode: str, base: int | None, ext: str = "png") -> str:
    if mode not in MODES:
        raise ValueError("未知模式 %r，只能是 %s" % (mode, "/".join(MODES)))
    if mode == "gen" or base is None:
        return "%s_v%d_%s.%s" % (theme, version, mode, ext)
    return "%s_v%d_%s_v%d.%s" % (theme, version, mode, base, ext)


def register(out_dir: str, theme: str, version: int, mode: str, base: int | None,
             filename: str, prompt: str = "", change: str = "", extra: dict | None = None) -> dict:
    """把一版登记进 manifest 并更新 当前版本.txt。返回这一版的记录。"""
    data = read_manifest(out_dir).get(theme) or {"theme": theme, "versions": {}}
    versions = data.get("versions") or {}

    # 连续 edit 计数：用于熔断判定（同一张图连续改了几次）
    consec = 0
    if mode == "edit" and base is not None:
        prev = versions.get("v%d" % base) or {}
        consec = int(prev.get("consecutive_edits") or 0) + 1
    # 累计编辑面积（由调用方传进来）
    cum_area = 0.0
    if mode == "edit" and base is not None:
        prev = versions.get("v%d" % base) or {}
        cum_area = float(prev.get("cumulative_edit_area_pct") or 0.0) + float((extra or {}).get("mask_area_pct") or 0.0)
    if mode in ("gen", "regen"):
        consec = 0
        cum_area = 0.0

    rec = {
        "file": filename,
        "mode": mode,
        "base": ("v%d" % base) if base else None,
        "round": version,
        "prompt": prompt,
        "change": change,
        "consecutive_edits": consec,
        "cumulative_edit_area_pct": round(cum_area, 1),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        rec.update({k: v for k, v in extra.items() if k not in rec})

    versions["v%d" % version] = rec
    data["theme"] = theme
    data["versions"] = versions
    data["current"] = "v%d" % version
    write_manifest(out_dir, theme, data)
    write_current(out_dir, filename)
    return rec


def lineage(theme: str, versions: dict, upto: int | None = None) -> str:
    """生成符号化谱系尾巴：v1🎲 → v2✏️ → v3✏️ → v4🎲(回v1重出)"""
    if not versions:
        return ""
    nums = sorted(int(k[1:]) for k in versions if k.startswith("v"))
    if upto:
        nums = [n for n in nums if n <= upto]
    icon = {"gen": "🎲", "regen": "🎲", "edit": "✏️", "adjust": "🎨"}
    parts = []
    for n in nums:
        r = versions.get("v%d" % n) or {}
        mode = r.get("mode", "gen")
        seg = "v%d%s" % (n, icon.get(mode, "?"))
        if mode == "regen" and r.get("base"):
            seg += "(回%s重出)" % r["base"]
        parts.append(seg)
    return " → ".join(parts)


def should_circuit_break(rec: dict, max_consecutive: int = 3, max_area: float = 40.0) -> tuple[bool, str]:
    """熔断判定：该不该强制走重新生成。"""
    c = int(rec.get("consecutive_edits") or 0)
    a = float(rec.get("cumulative_edit_area_pct") or 0.0)
    if c >= max_consecutive:
        return True, "这张图已连续局部改过 %d 次（上限 %d），再改会越改越糊" % (c, max_consecutive)
    if a >= max_area:
        return True, "累计改动面积已达 %.0f%%（上限 %.0f%%），已经不是「局部」了" % (a, max_area)
    return False, ""


# ---------------------------------------------------------------- 本地调色

# 预设：给"太暗了/太灰了"这类纯观感抱怨用的，零 API 调用
PRESETS = {
    "brighter": {"brightness": 1.20, "contrast": 1.05},
    "darker": {"brightness": 0.85},
    "punchier": {"contrast": 1.15, "saturation": 1.15},
    "softer": {"contrast": 0.92, "saturation": 0.92},
    "autocontrast": {"autocontrast": True},
}


def parse_adjust(spec: str) -> dict:
    """解析 --adjust 参数。

    支持两种写法（可混用，逗号分隔）：
      "brighter"                                  -> 用预设
      "brightness=1.25,contrast=1.1,gamma=1.1"    -> 精确值
    """
    ops: dict = {}
    for tok in (spec or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "=" not in tok:
            key = tok.lower()
            if key not in PRESETS:
                raise ValueError("未知预设 %r；可用：%s" % (tok, " / ".join(PRESETS)))
            ops.update(PRESETS[key])
            continue
        k, v = tok.split("=", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "autocontrast":
            ops["autocontrast"] = v.lower() in ("1", "true", "yes", "on")
        else:
            if k not in ("brightness", "contrast", "saturation", "sharpness", "gamma"):
                raise ValueError("未知调色项 %r；可用：brightness/contrast/saturation/sharpness/gamma/autocontrast" % k)
            ops[k] = float(v)
    if not ops:
        raise ValueError("调色参数为空")
    return ops


def describe_adjust(ops: dict) -> str:
    label = {"brightness": "亮度", "contrast": "对比度", "saturation": "饱和度",
             "sharpness": "锐度", "gamma": "伽马"}
    parts = []
    for k, v in ops.items():
        if k == "autocontrast":
            parts.append("自动对比度" + ("开" if v else "关"))
        else:
            parts.append("%s×%s" % (label.get(k, k), v))
    return "、".join(parts)


def apply_adjust(src: str, dst: str, ops: dict) -> dict:
    """纯本地调色，不动模型、不改构图、零劣化。返回处理结果。"""
    from PIL import Image, ImageEnhance

    im = Image.open(src)
    fmt = im.format or "PNG"
    before = im.size

    if ops.get("autocontrast"):
        from PIL import ImageOps
        im = ImageOps.autocontrast(im.convert("RGB"))
    if "gamma" in ops:
        g = float(ops["gamma"])
        if g <= 0:
            raise ValueError("gamma 必须 > 0")
        inv = 1.0 / g
        lut = [min(255, int(((i / 255.0) ** inv) * 255 + 0.5)) for i in range(256)]
        im = im.convert("RGB").point(lut * 3)
    for key, enh in (("brightness", ImageEnhance.Brightness),
                     ("contrast", ImageEnhance.Contrast),
                     ("saturation", ImageEnhance.Color),
                     ("sharpness", ImageEnhance.Sharpness)):
        if key in ops:
            im = enh(im).enhance(float(ops[key]))

    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    im.save(dst, format=fmt if fmt in ("PNG", "JPEG", "WEBP") else "PNG")
    return {"src": os.path.abspath(src), "dst": os.path.abspath(dst),
            "size_before": before, "size_after": im.size, "ops": ops}
