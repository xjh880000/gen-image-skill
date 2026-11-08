#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_image.py —— 生图客户端（单张 / 并发多张 / 自动降级 / 清晰报错）

这个包已经配好，开箱即用：
  · 接口地址：https://xyuapi.top/v1
  · 密钥：写在同目录上一级的 config.json 里（不在本文件里）
  · 默认模型：gpt-image-2.5-flare（实测 30.7 秒出图，画质最好）

用法：
  python gen_image.py -p "一只橘猫坐在窗台，午后暖光"
  python gen_image.py -p "同一句描述" -n 4              # 出 4 张
  python gen_image.py -p "图A" -p "图B" -p "图C"         # 三个描述各出一张
  python gen_image.py --prompts-file 提示词.txt -o 输出目录
  python gen_image.py --probe                          # 体检：哪些模型现在能用
  python gen_image.py --list-models                    # 看这个 key 能用哪些模型
  python gen_image.py -h                               # 全部参数

改配置（不用碰代码）：
  编辑 skill 根目录下的 config.json：
    {"base": "https://xyuapi.top/v1", "key": "sk-...", "model": "gpt-image-2.5-flare"}
  也可以用环境变量临时覆盖：GEN_IMAGE_KEY / GEN_IMAGE_BASE / GEN_IMAGE_MODEL

降级链（主力挂了自动换下一个，不用管）：
  gpt-image-2.5-flare  →  NanoBanana-Pro  →  gpt-image-2-pro  →  gpt-image-2.5-sunburst

出图后：
  · 图片存到 -o 指定的目录（默认 ./gen-image-out/）
  · 每张都会记版本，可以回看 / 迭代
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

# 同目录下的版本管理 / 本地调色模块（不涉及网络）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import version_store  # noqa: E402

# 管道/重定向时强制 UTF-8，避免中文报错被按 GBK 编码后变乱码
if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ===== 配置来源优先级：环境变量 > skill 目录下的 config.json > 内置默认 =====
# 给她用的包：key 写在 config.json 里，改配置不用碰代码。
def _load_skill_config():
    """读 skill 根目录下的 config.json；不存在或读不了就返回空 dict（不抛异常）。"""
    try:
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _p = os.path.join(_root, "config.json")
        if os.path.isfile(_p):
            with open(_p, "r", encoding="utf-8") as _f:
                _d = json.load(_f)
            return _d if isinstance(_d, dict) else {}
    except Exception:
        pass
    return {}


_CFG = _load_skill_config()

DEFAULT_BASE = os.environ.get("GEN_IMAGE_BASE") or _CFG.get("base") or "https://xyuapi.top/v1"
DEFAULT_KEY = os.environ.get("GEN_IMAGE_KEY") or _CFG.get("key") or ""
DEFAULT_MODEL = os.environ.get("GEN_IMAGE_MODEL") or _CFG.get("model") or "gpt-image-2.5-flare"
DEFAULT_SIZE = os.environ.get("GEN_IMAGE_SIZE", "1024x1024")
# 官方文档：size 档位（1K/2K/3K/4K）配合 ratio 使用，ratio 默认 1:1。
# 不传 ratio 就永远出正方形（2026-09-27 实测：1K=1024x1024，1K+16:9=1312x736）
DEFAULT_RATIO = os.environ.get("GEN_IMAGE_RATIO", "16:9")
VALID_RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9")

# 降级链：全部实测过能出图（2026-09 探测）
# ★★★ 2026-09-29 第三次迭代：**Qwen-Image-2.1 提到第一位**（用户明确要求）。
#   用户原话：「我们的skill要迭代换成用这个qwen2.1为主了嘛…优先用这个的。因为这个模型强啊对吧。」
#
#   为什么它能当主力（本次实测，不是听说）：
#     · 出图质量：同一句提示词（3:4 玄幻鲸鱼 / 宫崎骏风）一次出图即达标，构图与质感明显更强
#     · **图生图是真·局部编辑**：实测「给这只猫戴红帽子」只有 8~11% 像素变化，
#       背景像素级保留（商汤/agnes 达不到这个干净度）
#     · **能改位置**（这是老模型的死穴）：实测「把猫移到画面左侧，右边留空墙」真的挪了，
#       而商汤/agnes 类接口只会把灯头转个角度敷衍
#     · 支持**多图输入**（image_url 传数组）、公网 URL 与 data URI **都吃**
#     · 单张约 15~20 秒（实测 17.7s / 17.9s），比商汤的 30s 快
#
#   ⚠️ 走中转，别直连：
#      部分上游原生接口是**异步任务制**（只回 task_id 不回图），
#      需要额外轮询 /v1/tasks/{id} 才拿得到图。中转已内置轮询，
#      （见 xf-proxy commit a2b6846），所以这里当普通 OpenAI 接口用就行。
#   ⚠️ 图生图：部分上游没有 /v1/images/edits，参考图走**顶层 image_url 数组**，
#      由中转的 `json_image_url` 形态负责翻译（供应商「摩搭-生图」的图生图方式已设成它）。
#   ⚠️ 出图尺寸：实测恒出 1024×1024（不吃 size/ratio），要竖版只能事后裁或改提示词排版。
DEFAULT_CHAIN = _CFG.get("chain") or [
    # ===== 这个 key 能用的生图模型（实测数据见下）=====
    # gpt-image-2.5-flare   30.7s  1545 KB  ← 最快 + 画质最好，默认主力
    # gpt-image-2-pro       45.7s   253 KB  ← 兜底
    # NanoBanana-Pro        81.3s   484 KB  ← 慢，但质量稳，放第二
    "gpt-image-2.5-flare",     # ← 主力
    "NanoBanana-Pro",          # ← 次选
    "gpt-image-2-pro",         # ← 兜底
    "gpt-image-2.5-sunburst",  # ← 同族备用（未实测，留一格防全挂）
]

# ⛔ 受限模型：未经用户明确许可不得使用（2026-09-26 用户原话：
#    "gpt-image-2-pro、gemini-3-pro-image-preview这两个不要用。没我允许不要随便用。"）
#    ★ 拦截写在**代码**里而不是只写在 SKILL.md 里 —— 文档会被忽略，代码不会。
#      原先 gemini-3-pro-image-preview 就在 DEFAULT_CHAIN 第 3 位，
#      等于"agnes 一挂就自动用上被禁用的模型"，必须从链里摘掉。
RESTRICTED_MODELS = {
    # 这个包里没有禁用任何模型 —— 全部放行，她自己想用哪个用哪个。
}

# ⏱ 每个模型的「最低可用超时」（秒）—— 2026-11-06 新增。
#   为什么需要：`--timeout` 默认 90s 是给 13~31s 的老模型定的；
#   而 `NanoBanana-Pro` 空跑一句 "a red dot" 都要 **81s**，实拍真实提示词同样慢
#   → 用 90s 去卡它属于**必然的偶发误杀**（上游一抖就 ReadTimeout，白等 90s 还降级到弱模型）。
#   策略：链里出现某模型时，把本次超时**自动上调到该模型的最小值**，不给用户添心智负担。
#   ⚠️ 只上调、不下调：用户显式传更大的 --timeout 时尊重用户。
MODEL_MIN_TIMEOUT = {
    "gpt-image-2.5-flare": 90,     # 实测 30.7s，90s 留三倍余量
    "NanoBanana-Pro": 180,         # 实测 81.3s，这个模型天生慢，给足
    "gpt-image-2-pro": 120,        # 实测 45.7s
    "gpt-image-2.5-sunburst": 120, # 未实测，按同族保守给
}


def _restricted_hit(model_name):
    """返回命中的受限模型名；没命中返回 None。子串匹配（覆盖 (副本)/[豆包] 等后缀）。"""
    n = (model_name or "").lower()
    for r in RESTRICTED_MODELS:
        if r.lower() in n:
            return r
    return None


def filter_restricted(chain, allow=False):
    """把受限模型从降级链里摘掉。allow=True（用户当场许可）时原样放行。"""
    if allow:
        return chain
    kept, dropped = [], []
    for m in chain:
        (dropped if _restricted_hit(m) else kept).append(m)
    if dropped:
        print("⛔ 已从降级链摘掉受限模型（需用户明确许可）：%s" % ", ".join(dropped))
        print("   （要临时启用：加 --allow-restricted；但请先跟用户确认）")
    return kept

# 错误分类：接口所有错误都只有 {"error":{"type":"server_error","message":"中文说明"}}
# 没有可用的 code 字段，所以只能按 message 关键词判类
ERROR_RULES = [
    ("QUOTA", "额度已用完", "额度已用完（免费/试用额度耗尽，需要充值或换模型）"),
    ("AUTH", "认证失败", "鉴权失败（API Key 无效或未授权）"),
    ("RISK", "风控", "被上游风控拦截（提示词可能命中敏感内容，或上游临时抽风）"),
    ("PARAM", "请求参数无效", "模型名或参数无效"),
    ("PARAM", "模型无效", "模型名无效"),
]


def classify(http: int | str, message: str) -> tuple[str, str]:
    """把 HTTP 状态 + 中文报错归成一个稳定的错误码。

    ★修(2026-11-06)：**判定顺序改为「HTTP 状态优先，关键词其次」**。
      旧逻辑是先逐个匹配 message 关键词、再才看 HTTP 状态 —— 实测踩坑：
      `sensenova-u1-fast` 返回 **HTTP 404**，报文却是那句通用的
      「上游风控/输入内容超过模型最大token请缩减…」→ 被误判成 `RISK`（提示词风控），
      于是排查方向全歪（去改措辞，其实是**模型名/参数无效**）。
      现在：额度/认证/参数(400·404)/限流(429) 先按状态定死，最后才让关键词兜底。
    """
    msg = message or ""
    # ① 额度类：无论状态码是什么，只要明说额度用完就是 QUOTA
    if "额度已用完" in msg or "额度用完" in msg:
        return "QUOTA", "额度已用完（免费/试用额度耗尽，需要充值或换模型）"
    # ② 认证类
    if "认证失败" in msg or http == 401:
        return "AUTH", "鉴权失败（API Key 无效或未授权）"
    # ③ 参数/模型名类：400/404 且报文没明说风控 —— 状态码说话
    if http in (400, 404) and "风控" not in msg:
        return "PARAM", "请求参数或模型名无效（HTTP %s）" % http
    # ④ 限流类：429 单独给码（能重试、要换措辞/稍等，与"内容被拦"不是一回事）
    if http == 429:
        return "RATE", "上游限流/被拦（HTTP 429）——多是请求密集或输入过长，换措辞或稍后重试"
    # ⑤ 关键词兜底
    for code, kw, desc in ERROR_RULES:
        if kw in msg:
            return code, desc
    if http in (400, 404):
        return "PARAM", "请求参数或模型名无效（HTTP %s）" % http
    if http in (502, 503, 504) or not msg:
        return "GATEWAY", "网关超时/无响应（HTTP %s）——该模型这次太慢或上游挂了" % http
    if isinstance(http, int) and http >= 500:
        return "UPSTREAM", "上游服务错误（HTTP %s）" % http
    if http == "EXC":
        return "NETWORK", "网络异常/请求超时"
    return "UNKNOWN", msg or ("HTTP %s" % http)


def slug(s: str, maxlen: int = 24) -> str:
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", s).strip("_")
    return (s[:maxlen] or "img")


def png_dims(raw: bytes) -> str | None:
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        w, h = struct.unpack(">II", raw[16:24])
        return "%dx%d" % (w, h)
    except Exception:
        return None


def guess_ext(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return "bin"


def print_inline_markdown(path, caption=""):
    """★新增(2026-09-29)：打印**可直接粘贴进回复正文的 markdown 图片行**。

    为什么必须由脚本打印，而不是指望 AI 自己记得：
      **DSH 的对话不会自动显示图片** —— 只有 AI 在回复正文里写了 `![]()` 才会渲染
      （全局规则 39【图片内联显示铁律】：Codex 是"生成了图就自动显示"，DSH 不是）。
      实测教训（2026-09-29）：用户原话「**你只是最终成品才给我看到，但你过程中的我也应该看到**」
      —— 根因就是脚本只打印路径，AI 便只在最后一次回复里贴了图，中间几版全靠嘴描述。
      所以：**每次出图都打印这一行，让 AI 直接复制过去，不依赖自觉。**

    写法要求（踩了就是"图不显示"，而且**不会有任何报错**）：
      · 路径**必须带图片扩展名**（DSH 按扩展名判 MIME，没有会被 nosniff 拒绝渲染）
      · 路径**必须用尖括号 `<...>` 包住**（文件名含空格/中文时否则会被当普通文字）
      · 路径里不能有 `?` 或 `#`；单张不超过 20MB
    """
    p = os.path.abspath(path)
    cap = caption or os.path.basename(p)
    print("  \U0001f4cb 贴给用户看（**直接复制这一行**）: ![%s](<%s>)" % (cap, p))


# 海报/信息图类提示词的「文字量」预警阈值（实测得来，见 warn_if_prompt_too_long）
_TEXT_HEAVY_PROMPT_CHARS = 900


def warn_if_prompt_too_long(prompt):
    """★新增(2026-09-29)：提示词太长时提前预警 —— **海报/信息图会糊成一团**。

    实测（Qwen-Image-2.1，**同一套**「订婚致辞流程」内容，同一路径同一模型同一尺寸）：
      · **518 字**（只留 主标题 + 21 条短条目）                  → ✅ 干净海报，21 条文字只错 1 个数字
      · **1441 字**（原始完整版，多出「开场话术」「简短参考」      → ❌ 文字互相叠压、糊成一团，完全不可用
        「朋友致辞参考」「祝酒词」四大段台词）
    根因：模型会尝试把提示词里的**每一句话**都画上去；画布塞不下 → 文字重叠、字形崩坏。
    **不是模型能力问题，也不是中转 / 端口 / 尺寸问题** —— 换哪条路都一样糊。

    判定：>900 字就提醒。**只提醒、不擅自改用户内容**
    （静默截断提示词 = 篡改输入，比糊图更危险）。
    """
    n = len(prompt or "")
    if n < _TEXT_HEAVY_PROMPT_CHARS:
        return
    print("\u26a0\ufe0f 提示词偏长（%d 字）—— 海报/信息图类**文字量超过画布容量就会糊成一团**。" % n)
    print("   实测同一套内容：518 字 → 干净可用；1441 字（含大段台词）→ 文字叠压、完全不可用。")
    print("   → 删掉「台词 / 话术 / 参考范文」这类**给人念的**段落，只留要印在海报上的"
          "标题 + 条目，再试一次。")
    print("   （模型会把提示词里每句话都往上画，画布塞不下就重叠。）")


class Client:
    """生图接口客户端。

    注意 trust_env：Windows 上 requests 会静默读取「系统代理（注册表 Internet Settings）」
    走本地代理（实测走到 127.0.0.1:7890），即使 HTTP_PROXY 环境变量是空的。
    实测直连更快也更稳（20.6s / 25.2s，走代理 25~31s 且偶发 90s 超时），
    所以默认 trust_env=False（忽略一切代理，直连），需要时用 --use-proxy 打开。
    """

    def __init__(self, base: str, key: str, timeout: float, use_proxy: bool = False):
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = bool(use_proxy)

    def _headers(self) -> dict:
        return {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}

    def models(self) -> list[str]:
        r = self.session.get(self.base + "/models", headers=self._headers(), timeout=20)
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", [])]

    def generate(self, model: str, prompt: str, size: str, ratio: str = "",
                 timeout: float | None = None) -> dict:
        """返回 {ok, raw_bytes|None, http, message, seconds}

        ratio 必须与档位式 size（1K/2K/3K/4K）配合；不传 = 服务端默认 1:1。
        timeout：按模型覆盖单请求超时（None = 用 client 默认）。★2026-11-06 新增 ——
        「天生就慢」的模型（如 NanoBanana-Pro ~81s）不能和 30s 的模型共用同一个 90s 阈值。
        """
        to = self.timeout if timeout is None else float(timeout)
        t0 = time.time()
        payload = {"model": model, "prompt": prompt, "n": 1, "size": size}
        if ratio:
            payload["ratio"] = ratio
        try:
            r = self.session.post(self.base + "/images/generations", headers=self._headers(),
                                  data=json.dumps(payload).encode("utf-8"), timeout=to)
        except Exception as e:
            return {"ok": False, "http": "EXC", "message": repr(e),
                    "seconds": round(time.time() - t0, 1)}

        secs = round(time.time() - t0, 1)
        if r.status_code != 200:
            try:
                j = r.json()
                msg = (j.get("error") or {}).get("message") or json.dumps(j, ensure_ascii=False)
            except Exception:
                msg = r.text[:300]
            return {"ok": False, "http": r.status_code, "message": msg, "seconds": secs}

        try:
            j = r.json()
        except Exception:
            return {"ok": False, "http": 200, "message": "返回体不是 JSON：" + r.text[:200], "seconds": secs}

        data = j.get("data") or []
        if not data:
            return {"ok": False, "http": 200, "message": "返回 200 但没有 data 字段", "seconds": secs}

        d = data[0]
        if d.get("b64_json"):
            try:
                raw = base64.b64decode(d["b64_json"])
            except Exception as e:
                return {"ok": False, "http": 200, "message": "base64 解码失败：" + repr(e), "seconds": secs}
        elif d.get("url"):
            try:
                rr = self.session.get(d["url"], timeout=to)
                rr.raise_for_status()
                raw = rr.content
            except Exception as e:
                return {"ok": False, "http": 200, "message": "下载图片 URL 失败：" + repr(e), "seconds": secs}
        else:
            return {"ok": False, "http": 200,
                    "message": "返回里既没有 b64_json 也没有 url，字段=" + str(sorted(d.keys())),
                    "seconds": secs}

        if len(raw) < 512:
            return {"ok": False, "http": 200, "message": "图片数据过小（%d 字节），疑似空图" % len(raw),
                    "seconds": secs}
        return {"ok": True, "raw": raw, "http": 200, "message": "OK", "seconds": secs}

    def edit(self, model: str, prompt: str, image_path: str, size: str) -> dict:
        """图生图 / 指令编辑。按两条路径依次尝试：
             路径1  POST /images/edits      （OpenAI 标准，multipart/form-data）
             路径2  POST /chat/completions  （多模态消息，图以 data URL 内嵌）
        两条都不通时返回 http="EDIT_UNAVAILABLE"，由调用方决定是否回退到重新生成。
        """
        t0 = time.time()
        tried: list[dict] = []
        try:
            with open(image_path, "rb") as f:
                img_bytes = f.read()
        except Exception as e:
            return {"ok": False, "http": "LOCAL", "message": "读不到源图：%r" % e,
                    "seconds": 0.0, "tried": []}
        if len(img_bytes) < 512:
            return {"ok": False, "http": "LOCAL", "message": "源图文件过小（%d 字节）" % len(img_bytes),
                    "seconds": 0.0, "tried": []}

        # ---- 路径1：/images/edits（multipart）----
        try:
            files = {"image": (os.path.basename(image_path), img_bytes, "image/png")}
            form = {"model": model, "prompt": prompt, "n": "1", "size": size}
            r = self.session.post(self.base + "/images/edits",
                                  headers={"Authorization": "Bearer " + self.key},
                                  files=files, data=form, timeout=self.timeout)
            tried.append({"/images/edits": "HTTP %s | %s" % (r.status_code, r.text[:160].replace("\n", " "))})
            if r.status_code == 200:
                raw, how = extract_image(r, self.session, self.timeout)
                if raw and len(raw) >= 512:
                    return {"ok": True, "raw": raw, "http": 200,
                            "message": "OK via /images/edits (%s)" % how,
                            "seconds": round(time.time() - t0, 1), "tried": tried, "path": "/images/edits"}
                tried.append({"/images/edits 解析": how})
        except Exception as e:
            tried.append({"/images/edits": "EXC " + repr(e)})

        # ---- 路径2：/chat/completions（多模态）----
        b64 = base64.b64encode(img_bytes).decode("ascii")
        payload = {"model": model, "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ]}]}
        try:
            r = self.session.post(self.base + "/chat/completions", headers=self._headers(),
                                  data=json.dumps(payload).encode("utf-8"), timeout=self.timeout)
            tried.append({"/chat/completions": "HTTP %s | %s" % (r.status_code, r.text[:160].replace("\n", " "))})
            if r.status_code == 200:
                raw, how = extract_image(r, self.session, self.timeout)
                if raw and len(raw) >= 512:
                    return {"ok": True, "raw": raw, "http": 200,
                            "message": "OK via /chat/completions (%s)" % how,
                            "seconds": round(time.time() - t0, 1), "tried": tried, "path": "/chat/completions"}
                tried.append({"/chat/completions 解析": how})
        except Exception as e:
            tried.append({"/chat/completions": "EXC " + repr(e)})

        return {"ok": False, "http": "EDIT_UNAVAILABLE",
                "message": "两条编辑路径都不通（接口很可能还没支持图生图/图片编辑）",
                "seconds": round(time.time() - t0, 1), "tried": tried}


def extract_image(resp, session, timeout: float) -> tuple[bytes | None, str]:
    """从响应里抠出图片字节。兼容两种返回：
       A) OpenAI 生图格式  {"data":[{"b64_json": ...} | {"url": ...}]}
       B) 对话格式        choices[0].message.content 里内嵌 markdown data URL
    返回 (raw_bytes | None, 说明)
    """
    try:
        j = resp.json()
    except Exception:
        return None, "返回体不是 JSON：" + resp.text[:200]

    # A) OpenAI 生图格式
    data = j.get("data")
    if isinstance(data, list) and data:
        d = data[0]
        if d.get("b64_json"):
            try:
                return base64.b64decode(d["b64_json"]), "b64_json"
            except Exception as e:
                return None, "base64 解码失败：" + repr(e)
        if d.get("url"):
            try:
                rr = session.get(d["url"], timeout=timeout)
                rr.raise_for_status()
                return rr.content, "url"
            except Exception as e:
                return None, "下载图片 URL 失败：" + repr(e)
        return None, "data[0] 里既没有 b64_json 也没有 url，字段=" + str(sorted(d.keys()))

    # B) 对话格式：content 里内嵌 data:image/...;base64,xxx
    try:
        content = j["choices"][0]["message"]["content"]
    except Exception:
        return None, "既不是生图格式也不是对话格式：" + json.dumps(j, ensure_ascii=False)[:300]
    if isinstance(content, list):  # 有些实现 content 是数组
        content = " ".join(
            (c.get("text") or c.get("image_url", {}).get("url") or "") if isinstance(c, dict) else str(c)
            for c in content)
    m = re.search(r"data:image/(?:png|jpeg|jpg|webp);base64,([A-Za-z0-9+/=\s]+)", content or "")
    if not m:
        return None, "对话返回里没找到内嵌图片。正文前 200 字：" + (content or "")[:200]
    try:
        return base64.b64decode(re.sub(r"\s+", "", m.group(1))), "chat-data-url"
    except Exception as e:
        return None, "对话内嵌图片 base64 解码失败：" + repr(e)


def _is_square(dims) -> bool:
    """dims 形如 '1024x1024'。用于检测 ratio 静默失效（实测商汤/agnes 都不吃 ratio）。"""
    try:
        w, h = str(dims).lower().split("x")
        return int(w) == int(h)
    except Exception:
        return False


def run_task(idx: int, prompt: str, chain: list[str], size: str, client: Client,
             retries: int, out_dir: str, prefix: str, ts: str, lock, ratio: str = "") -> dict:
    """一个任务 = 一张图。按降级链逐个试模型，同一条链最多跑 retries+1 遍。"""
    attempts = []
    final_code, final_desc = "UNKNOWN", ""

    for round_no in range(retries + 1):
        transient = False
        for model in chain:
            # ⏱ 按模型给超时：慢模型用 MODEL_MIN_TIMEOUT 的下限，快模型仍用全局默认（2026-11-06）
            _to = max(client.timeout, MODEL_MIN_TIMEOUT.get(model, 0) or 0)
            res = client.generate(model, prompt, size, ratio, timeout=_to)
            code, desc = (None, None) if res["ok"] else classify(res["http"], res["message"])
            attempts.append({
                "model": model, "round": round_no + 1, "ok": res["ok"],
                "http": res["http"], "seconds": res["seconds"],
                "error_code": code, "error_desc": desc,
                "raw_message": None if res["ok"] else res["message"],
            })
            if res["ok"]:
                raw = res["raw"]
                ext = guess_ext(raw)
                name = "%02d_%s_%s.%s" % (idx, slug(prefix or model), ts, ext)
                path = os.path.join(out_dir, name)
                with lock:
                    with open(path, "wb") as f:
                        f.write(raw)
                return {
                    "index": idx, "prompt": prompt, "ok": True,
                    "model_used": model, "file": os.path.abspath(path),
                    "bytes": len(raw), "dims": png_dims(raw), "seconds": res["seconds"],
                    "attempts": attempts,
                    # 降级标记：不是链首模型出的图 = 首选挂了/额度用尽（2026-09-28 加）
                    "fallback": model != chain[0],
                    "chain_head": chain[0],
                }
            final_code, final_desc = code, desc
            if code in ("GATEWAY", "UPSTREAM", "NETWORK"):
                transient = True
        if not transient:
            break

    return {
        "index": idx, "prompt": prompt, "ok": False,
        "error_code": final_code, "error_desc": final_desc,
        "seconds": None, "attempts": attempts,
    }


def _resolve_source(args, theme: str | None) -> str | None:
    """找出这次迭代要基于哪张图。优先 --edit，其次该主题的当前版本。"""
    if args.edit:
        return os.path.abspath(args.edit)
    if theme:
        vs = version_store.parse_versions(args.out, theme)
        if vs:
            return vs[-1]["path"]
    return None


def run_lineage(args) -> int:
    """打印某主题的版本谱系。"""
    if not args.theme:
        print("--lineage 需要同时给 --theme")
        return 2
    data = version_store.read_manifest(args.out).get(args.theme) or {}
    versions = data.get("versions") or {}
    if not versions:
        print("主题「%s」在 %s 下没有任何版本记录" % (args.theme, os.path.abspath(args.out)))
        return 1
    cur = data.get("current")
    print("主题：%s    当前：%s    目录：%s" % (args.theme, cur, os.path.abspath(args.out)))
    print("-" * 78)
    for n in sorted(int(k[1:]) for k in versions):
        r = versions["v%d" % n] or {}
        mark = " ← 当前" if ("v%d" % n) == cur else ""
        icon = {"gen": "🎲", "regen": "🎲", "edit": "✏️", "adjust": "🎨"}.get(r.get("mode"), "?")
        print("  v%-3d %s %-6s 基于%-5s %s%s" % (
            n, icon, r.get("mode", "?"), r.get("base") or "—", r.get("ts", ""), mark))
        if r.get("change"):
            print("        改动：%s" % r["change"])
        if r.get("prompt"):
            print("        提示词：%s" % r["prompt"][:110])
        if r.get("mode") == "edit" and r.get("consecutive_edits"):
            print("        连续编辑：%d 次   累计改动面积：%s%%" % (
                r["consecutive_edits"], r.get("cumulative_edit_area_pct")))
    print("-" * 78)
    print("谱系：%s" % version_store.lineage(args.theme, versions))
    return 0


def run_adjust_mode(args) -> int:
    """本地调色模式：不走模型、不改构图、零劣化。用于「太暗了/太灰了/看不清」这类纯观感抱怨。"""
    theme = args.theme
    src = _resolve_source(args, theme)
    if not src or not os.path.isfile(src):
        print("本地调色需要一张源图：用 --edit <原图> 指定，或用 --theme 让脚本自动取该主题当前版本。")
        return 2

    try:
        ops = version_store.parse_adjust(args.adjust)
    except ValueError as e:
        print("调色参数错误：%s" % e)
        return 2

    os.makedirs(args.out, exist_ok=True)
    if theme:
        ver = version_store.next_version(args.out, theme)
        base_ver = args.from_ver if args.from_ver is not None else version_store.version_of_file(src)
        name = version_store.make_filename(theme, ver, "adjust", base_ver, "png")
    else:
        ver, base_ver = 1, None
        name = "%s%s_adjust_%s.png" % (args.prefix, "adjust" if not args.prefix else "", datetime.now().strftime("%H%M%S"))
    dst = os.path.join(os.path.abspath(args.out), name)

    print("本地调色（不调模型、不改构图）：")
    print("  源图：%s" % src)
    print("  操作：%s" % version_store.describe_adjust(ops))
    try:
        info = version_store.apply_adjust(src, dst, ops)
    except Exception as e:
        print("调色失败：%r" % e)
        return 2

    print("  输出：%s" % info["dst"])
    print_inline_markdown(info["dst"], "本地调色结果")
    print("  尺寸：%sx%s（与源图一致=%s）" % (info["size_after"][0], info["size_after"][1],
                                        info["size_after"] == info["size_before"]))
    if theme:
        rec = version_store.register(args.out, theme, ver, "adjust", base_ver, name,
                                     prompt="(本地调色，无提示词)",
                                     change=args.change or version_store.describe_adjust(ops))
        print("  版本：v%d（基于 %s）" % (ver, rec["base"] or "—"))
        print("  谱系：%s" % version_store.lineage(theme, version_store.read_manifest(args.out)[theme]["versions"]))
    return 0


def run_edit_mode(args, client: Client, chain: list[str]) -> int:
    """渐进编辑模式：在已有图上局部改。接口没通时明确报错，不静默回退。"""
    theme = args.theme
    src = _resolve_source(args, theme)
    if not src or not os.path.isfile(src):
        print("渐进编辑需要源图：用 --edit <原图> 指定，或用 --theme 自动取当前版本。")
        return 2
    if not args.prompt:
        print("渐进编辑需要 --prompt 说明「改什么」。")
        return 2

    print("渐进编辑（基于已有图）：")
    print("  源图：%s" % src)
    print("  指令：%s" % args.prompt[0])
    print("  模型：%s" % chain[0])
    # ★新增(2026-09-29)：编辑**默认沿用源图尺寸**，别用全局默认值把竖版图改成方图。
    #   以前无所谓 —— 商汤/agnes 根本不吃 `size`，恒出方图，传什么都一样；
    #   但换成 Qwen-Image-2.1 之后 `size` 是**真生效**的：
    #   实测一张 768×1024 的竖版海报，编辑一次就变成 1024×1024 方图（版式全被重排）。
    #   只在用户**没显式传 --size** 时才沿用源图，显式传了就以用户为准。
    edit_size = args.size
    if edit_size == DEFAULT_SIZE:
        try:
            with open(src, "rb") as _f:
                _dims = png_dims(_f.read(32))
        except Exception:
            _dims = None
        if _dims:
            edit_size = _dims
            print("  尺寸：沿用源图 %s（想改成别的尺寸就显式传 --size）" % edit_size)
        else:
            print("  尺寸：源图不是 PNG，读不到尺寸 → 用默认 %s" % edit_size)
    res = client.edit(chain[0], args.prompt[0], src, edit_size)

    for t in res.get("tried", []):
        for k, v in t.items():
            print("  试过 %s -> %s" % (k, v))

    if not res["ok"]:
        print("-" * 78)
        print("❌ 渐进编辑没成功：%s" % res["message"])
        if res.get("http") == "EDIT_UNAVAILABLE":
            print("   判断：接口还没支持图生图/图片编辑（两条路径都不通）。")
            print("   建议：改用「重新生成」——优化提示词后重出一版（构图会变，但能修结构问题）。")
            print("   命令示例：python gen_image.py -p \"<优化后的提示词>\" --theme \"%s\" --mode regen --from-ver %s"
                  % (theme or "主题名", (version_store.parse_versions(args.out, theme)[-1]["version"]
                                      if theme and version_store.parse_versions(args.out, theme) else 1)))
        return 3

    os.makedirs(args.out, exist_ok=True)
    ext = guess_ext(res["raw"])
    if theme:
        ver = version_store.next_version(args.out, theme)
        # 基于哪一版必须看真实源图，不能用「当前版本」顶替，否则谱系记错、回退会回错
        base_ver = args.from_ver if args.from_ver is not None else version_store.version_of_file(src)
        name = version_store.make_filename(theme, ver, "edit", base_ver, ext)
    else:
        ver, base_ver = 1, None
        name = "%sedit_%s.%s" % (args.prefix, datetime.now().strftime("%H%M%S"), ext)
    dst = os.path.join(os.path.abspath(args.out), name)
    with open(dst, "wb") as f:
        f.write(res["raw"])

    print("-" * 78)
    print("✅ 渐进编辑成功：%ss  通道=%s" % (res["seconds"], res.get("path")))
    print("   输出：%s" % dst)
    print_inline_markdown(dst, (args.change or args.prompt[0] or "编辑结果"))
    if theme:
        rec = version_store.register(args.out, theme, ver, "edit", base_ver, name,
                                     prompt=args.prompt[0], change=args.change or args.prompt[0],
                                     extra={"channel": res.get("path"), "mask_area_pct": None})
        print("   版本：v%d（基于 %s）  连续编辑 %d 次" % (ver, rec["base"] or "—", rec["consecutive_edits"]))
        brk, why = version_store.should_circuit_break(rec, args.max_edits, args.max_area)
        if brk:
            print("   ⚠️ 熔断预警：%s —— 下一次建议改用重新生成" % why)
        print("   谱系：%s" % version_store.lineage(theme, version_store.read_manifest(args.out)[theme]["versions"]))
    return 0


def run_gallery(args) -> int:
    """扫描输出目录，生成 gallery.js（供 gallery.html 加载）。

    为什么要拆成 .js 而不是直接写 .html：
      DSH 的「产出文件」按钮只收录 write/edit 工具写的文件，脚本生成的不算。
      所以脚本只生成数据（gallery.js），由 agent 用 write 工具写一个固定的
      gallery.html 外壳 —— 这样按钮才点得动，而且 agent 不用把图片清单塞进 token。
    """
    d = os.path.abspath(args.gallery or args.out)
    if not os.path.isdir(d):
        print("目录不存在：%s" % d)
        return 2
    exts = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    files = sorted(f for f in os.listdir(d) if f.lower().endswith(exts))
    if not files:
        print("目录里没有图片：%s" % d)
        return 1

    items = []
    for f in files:
        p = os.path.join(d, f)
        label, desc = "", ""
        # 从文件名里猜语义标签（我用描述性命名时会带「正确」「错误」）
        if "正确" in f:
            label = "ok"
        elif "错误" in f:
            label = "bad"
        # 去掉扩展名和前缀数字，作为标题
        title = os.path.splitext(f)[0]
        title = re.sub(r"^\d+[_\-]", "", title).replace("_", " ")
        try:
            with open(p, "rb") as fh:
                dims = png_dims(fh.read(64))
        except Exception:
            dims = None
        items.append({
            "file": f, "title": title, "label": label, "desc": desc,
            "bytes": os.path.getsize(p), "dims": dims,
        })

    payload = {
        "dir": d,
        "title": os.path.basename(d),
        "count": len(items),
        "items": items,
    }
    out = os.path.join(d, "gallery.js")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("window.GALLERY = ")
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write(";\n")

    ok_n = sum(1 for i in items if i["label"] == "ok")
    bad_n = sum(1 for i in items if i["label"] == "bad")
    print("已生成 %s" % out)
    print("  共 %d 张（正确 %d / 错误 %d / 未标注 %d）" % (
        len(items), ok_n, bad_n, len(items) - ok_n - bad_n))
    for i in items:
        print("    %-6s %s" % (i["label"] or "-", i["file"]))
    print("")
    print("下一步：用 write 工具在本目录创建 gallery.html（内容见 skill 的 assets/gallery.html），")
    print("        DSH 才会在对话底部显示可点击的产出按钮。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="第三方生图 API 客户端（单张/并发多张/自动降级/清晰报错）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-p", "--prompt", action="append", default=[],
                   help="提示词，可重复传多次（每次=一张图）")
    p.add_argument("--prompts-file", help="提示词文件，每行一个（# 开头为注释）")
    p.add_argument("-n", "--n", type=int, default=1, help="同一提示词出几张（默认 1，靠并发实现）")
    p.add_argument("-o", "--out", default="gen-image-out", help="输出目录（默认 ./gen-image-out）")
    p.add_argument("--prefix", default="", help="文件名前缀（不启用版本管理时用）")

    # ---- 迭代相关 ----
    it = p.add_argument_group("迭代 / 版本管理")
    it.add_argument("--theme", help="主题名。传了就启用版本管理，文件名变 {主题}_v{N}_{模式}_v{基于几}.png（只支持一次出一张）")
    it.add_argument("--mode", choices=("gen", "edit", "regen", "adjust"),
                    help="迭代模式（不传则自动推断：有 --adjust=adjust，有 --edit=edit，否则 gen）")
    it.add_argument("--edit", metavar="原图路径",
                    help="渐进编辑：在已有图上局部改（需要接口支持图生图；不支持时会明确报错并给出降级建议）")
    it.add_argument("--adjust", metavar="调色参数",
                    help="本地调色，不走模型、零劣化：'brighter' 或 'brightness=1.25,contrast=1.1'")
    it.add_argument("--change", default="", help="这一版改了什么（写进 manifest 便于回溯）")
    it.add_argument("--from-ver", type=int, help="显式指定基于哪个版本号（默认=该主题当前版本）")
    it.add_argument("--max-edits", type=int, default=3,
                    help="熔断：同一张图连续局部改几次后强制重新生成。默认 3——实测该接口是真局部编辑"
                         "（背景像素级保留），劣化只发生在改动区，所以可以改 3 次")
    it.add_argument("--max-area", type=float, default=40.0,
                    help="熔断：累计改动面积超百分之多少后强制重新生成（默认 40）")
    it.add_argument("--lineage", action="store_true", help="只打印某主题的版本谱系，不出图")
    it.add_argument("--gallery", nargs="?", const="", metavar="目录",
                    help="扫描目录生成 gallery.js（供 gallery.html 加载）；不传目录则用 --out")

    p.add_argument("-m", "--model", default=DEFAULT_MODEL, help="主模型")
    p.add_argument("--models", help="自定义降级链，逗号分隔（默认内置 4 个实测可用模型）")
    p.add_argument("--no-fallback", action="store_true", help="只用主模型，不降级")
    p.add_argument("--allow-restricted", action="store_true",
                   help="允许使用受限模型（gpt-image-2-pro / gemini-3-pro-image-preview）。"
                        "⚠️ 必须用户明确许可才能加，默认禁用")
    p.add_argument("--size", default=DEFAULT_SIZE, help="尺寸档位（建议 1K/2K/3K/4K，也接受 1024x1024）")
    p.add_argument("--ratio", default=DEFAULT_RATIO, choices=list(VALID_RATIOS) + [""],
                   help="宽高比（默认 16:9；竖屏用 9:16；传空串 = 服务端默认 1:1）")
    p.add_argument("-c", "--concurrency", type=int, default=12,
                   help="并发数（默认 12；2026-10-06 实测 12 并发 12/12 成功、单张 29~38s、"
                        "总耗时比 3 并发快近一倍。旧的「并发>3 会 502」结论已作废）")
    p.add_argument("--timeout", type=float, default=90,
                   help="单请求超时秒数（默认 90）。慢模型会自动用各自的下限（见 MODEL_MIN_TIMEOUT："
                        "NanoBanana-Pro 180s / gpt-image-2-pro 120s / gpt-image-2.5-flare 90s），不受这里影响")
    p.add_argument("--retries", type=int, default=1, help="整条降级链重跑次数（默认 1，仅对超时类错误重跑）")
    p.add_argument("--base", default=DEFAULT_BASE, help="接口地址（默认 %s）" % DEFAULT_BASE)
    p.add_argument("--key", default=DEFAULT_KEY, help="API Key")
    p.add_argument("--use-proxy", action="store_true",
                   help="走系统/环境代理（默认直连；Windows 上 requests 会静默读系统代理走 127.0.0.1:7890）")
    p.add_argument("--json", action="store_true", help="只输出 JSON")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不实际调用")
    p.add_argument("--probe", action="store_true", help="体检：并发测降级链里每个模型现在能不能用")
    p.add_argument("--list-models", action="store_true", help="列出接口上所有疑似生图的模型")
    return p


def main() -> int:
    args = build_parser().parse_args()

    # ---- 模式：本地调色（不走模型，零劣化）----
    if args.adjust:
        return run_adjust_mode(args)

    # ---- 模式：查谱系 ----
    if args.lineage:
        return run_lineage(args)

    # ---- 模式：生成画廊数据 ----
    if args.gallery is not None:
        return run_gallery(args)

    client = Client(args.base, args.key, args.timeout, use_proxy=args.use_proxy)

    # ---- 模式：列模型 ----
    if args.list_models:
        try:
            ids = client.models()
        except Exception as e:
            print("拉取模型列表失败：%r" % e)
            return 2
        kw = ("image", "画图", "绘图", "生图", "banana", "dall", "flux", "sensenova")
        hits = [i for i in ids if i and any(k.lower() in i.lower() for k in kw)]
        print("接口共 %d 个模型，疑似生图相关 %d 个：" % (len(ids), len(hits)))
        for i in hits:
            print("  - %s" % i)
        return 0

    # 优先级：--models（显式指定整条链） > --no-fallback（只用主模型） > 默认降级链
    if args.models:
        chain = [x.strip() for x in args.models.split(",") if x.strip()]
    elif args.no_fallback:
        chain = [args.model]
    else:
        chain = [args.model] + [m for m in DEFAULT_CHAIN if m != args.model]

    # ⛔ 受限模型兜底拦截：无论从哪条路径进来（--model / --models / 默认链），
    #    未经 --allow-restricted 一律摘掉。防止"agnes 一挂就自动用上被禁用的模型"。
    chain = filter_restricted(chain, allow=getattr(args, "allow_restricted", False))
    if not chain:
        print("错误：降级链里只剩下受限模型，已全部摘除。请换模型，或用 --allow-restricted（需用户许可）。")
        return 2

    # ⏱ 慢模型提示（2026-11-06 新增，见 MODEL_MIN_TIMEOUT 注释）：
    #   真正的超时上调发生在**每个模型自己那一跳**（run_task / probe 里按模型取下限），
    #   这里只做一句告知，**不改全局 client.timeout** —— 否则快模型会被慢模型的 180s 拖累。
    slow = [m for m in chain if MODEL_MIN_TIMEOUT.get(m, 0) and MODEL_MIN_TIMEOUT[m] > client.timeout]
    if slow:
        print("⏱ 慢模型提示：%s 单张实测较慢，该模型这一跳的超时会用到 %ss（其余模型仍按 %ss）"
              % ("/".join(slow), max(MODEL_MIN_TIMEOUT[m] for m in slow), int(client.timeout)))

    # ---- 模式：渐进编辑（在已有图上局部改）----
    if args.edit or args.mode == "edit":
        return run_edit_mode(args, client, chain)

    # ---- 模式：体检 ----
    if args.probe:
        print("体检降级链（%d 个并发）：%s" % (len(chain), " -> ".join(chain)))
        rows = []
        with ThreadPoolExecutor(max_workers=max(1, len(chain))) as ex:
            futs = {ex.submit(client.generate, m, "a small red dot on white background", args.size, "",
                              max(client.timeout, MODEL_MIN_TIMEOUT.get(m, 0) or 0)): m
                    for m in chain}
            for fu in as_completed(futs):
                m = futs[fu]
                r = fu.result()
                code, desc = (None, None) if r["ok"] else classify(r["http"], r["message"])
                rows.append((m, r, code, desc))
        rows.sort(key=lambda x: chain.index(x[0]))
        for m, r, code, desc in rows:
            if r["ok"]:
                print("  [可用] %-32s %ss  %s" % (m, r["seconds"], png_dims(r["raw"]) or ""))
            else:
                print("  [不可用] %-30s HTTP=%s %ss  错误码=%s  %s" % (m, r["http"], r["seconds"], code, desc))
                print("           原文：%s" % r["message"])
        return 0

    # ---- 组装任务 ----
    prompts = list(args.prompt)
    if args.prompts_file:
        with open(args.prompts_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    prompts.append(line)
    if not prompts:
        print("错误：没有提示词。用 -p \"...\" 或 --prompts-file 传入。")
        return 2

    # ★新增(2026-09-29)：文字量预警（海报/信息图塞太多字会糊成一团，见 warn_if_prompt_too_long）
    for _p in prompts:
        warn_if_prompt_too_long(_p)

    tasks = []
    for p in prompts:
        for _ in range(max(1, args.n)):
            tasks.append(p)

    # 版本管理只支持一次一张：迭代的本质是「一张图一版」，多张会让谱系无法自洽
    if args.theme and len(tasks) > 1:
        print("错误：--theme（版本管理）一次只能出一张图，这次要出 %d 张。" % len(tasks))
        print("      迭代的本质就是「一张图一版」。多张请用 --prefix 命名，或分多次调用。")
        return 2

    if args.dry_run:
        print("计划：共 %d 张图，并发 %d，输出到 %s" % (len(tasks), args.concurrency, os.path.abspath(args.out)))
        print("降级链：%s" % " -> ".join(chain))
        for i, t in enumerate(tasks, 1):
            print("  [%02d] %s" % (i, t[:80]))
        return 0

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    lock = __import__("threading").Lock()

    t_start = time.time()
    print("开始生图：%d 张，并发 %d，输出目录 %s" % (len(tasks), args.concurrency, out_dir))
    print("降级链：%s" % " -> ".join(chain))
    print("-" * 78)

    results = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = {ex.submit(run_task, i + 1, t, chain, args.size, client,
                          max(0, args.retries), out_dir, args.prefix, ts, lock): i
                for i, t in enumerate(tasks)}
        for fu in as_completed(futs):
            i = futs[fu]
            try:
                results[i] = fu.result()
            except Exception as e:
                results[i] = {"index": i + 1, "prompt": tasks[i], "ok": False,
                              "error_code": "INTERNAL", "error_desc": "脚本内部异常：" + repr(e),
                              "attempts": []}
            r = results[i]
            if r["ok"]:
                print("[%02d/%02d] 成功  %s  %ss  %s" % (
                    i + 1, len(tasks), r["model_used"], r["seconds"], r["file"]))
                if r.get("fallback"):
                    # ★改(2026-11-06)：文案不再写死"兜底模型中文不可用" —— 现在兜底1 是商汤
                    #   （中文文字反而最准），写死会误导。只客观说明"实际是哪个模型出的"。
                    print("      \u26a0\ufe0f 降级告警：首选 %s 这次没出图，本张由兜底模型 %s 生成"
                          "（画质/文字能力与首选有差异，中文文字图尤其要复查错字）。"
                          % (r.get("chain_head"), r["model_used"]))
            else:
                print("[%02d/%02d] 失败  错误码=%s  %s" % (
                    i + 1, len(tasks), r.get("error_code"), r.get("error_desc")))

    total = round(time.time() - t_start, 1)
    ok = [r for r in results if r and r["ok"]]
    bad = [r for r in results if not r or not r["ok"]]

    # ---- 版本登记（迭代谱系）----
    # 规则：同一主题已经有版本时，一次普通生图就是「重新生成」(regen) 而不是「首次生成」(gen)
    ver_line = ""
    if args.theme and len(ok) == 1 and not bad:
        r = ok[0]
        existing = version_store.parse_versions(out_dir, args.theme)
        mode = args.mode or ("gen" if not existing else "regen")
        if mode not in ("gen", "regen"):
            mode = "regen" if existing else "gen"
        ver = version_store.next_version(out_dir, args.theme)
        base_ver = args.from_ver
        if base_ver is None and existing:
            base_ver = max(v["version"] for v in existing)
        if mode == "gen":
            base_ver = None
        ext = (os.path.splitext(r["file"])[1].lstrip(".") or "png").lower()
        newname = version_store.make_filename(args.theme, ver, mode, base_ver, ext)
        newpath = os.path.join(out_dir, newname)
        try:
            os.replace(r["file"], newpath)
            r["file"] = newpath
            r["version"] = "v%d" % ver
            r["iter_mode"] = mode
            rec = version_store.register(out_dir, args.theme, ver, mode, base_ver, newname,
                                        prompt=r["prompt"], change=args.change,
                                        extra={"model_used": r.get("model_used")})
            vs = version_store.read_manifest(out_dir)[args.theme]["versions"]
            ver_line = "版本登记：v%d  模式=%s  基于=%s  连续编辑=%d" % (
                ver, mode, rec["base"] or "—", rec["consecutive_edits"])
            print("-" * 78)
            print(ver_line)
            print("  文件：%s" % newpath)
            print("  谱系：%s" % version_store.lineage(args.theme, vs))
            print("  当前版本已写入：%s" % os.path.join(out_dir, version_store.CURRENT_NAME))
        except Exception as e:
            # 图已经出好了，登记失败不能影响交付
            print("⚠️ 版本登记失败（图已生成，可正常使用）：%r" % e)

    # ---- 落盘结果（UTF-8，供精确定位）----
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "endpoint": args.base, "chain": chain, "size": args.size, "ratio": args.ratio,
        "out_dir": out_dir, "total": len(tasks),
        "ok_count": len(ok), "fail_count": len(bad), "elapsed_seconds": total,
        "theme": args.theme, "version_note": ver_line or None,
        "results": results,
    }
    json_path = os.path.join(out_dir, "result.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    txt = ["生图结果 %s" % summary["generated_at"], "=" * 78,
           "接口：%s" % args.base, "降级链：%s" % " -> ".join(chain),
           "总计 %d 张，成功 %d，失败 %d，耗时 %ss" % (len(tasks), len(ok), len(bad), total), ""]
    for r in results:
        if not r:
            continue
        txt.append("[%02d] %s" % (r["index"], "成功" if r["ok"] else "失败"))
        txt.append("     提示词：%s" % r["prompt"][:200])
        if r["ok"]:
            txt.append("     模型：%s   耗时：%ss   尺寸：%s   大小：%d 字节" % (
                r["model_used"], r["seconds"], r.get("dims"), r.get("bytes", 0)))
            if r.get("fallback"):
                txt.append("     \u26a0\ufe0f 降级：首选 %s 未出图，本张由兜底模型 %s 生成"
                           % (r.get("chain_head"), r["model_used"]))
            txt.append("     文件：%s" % r["file"])
        else:
            txt.append("     错误码：%s   说明：%s" % (r.get("error_code"), r.get("error_desc")))
        for a in r.get("attempts", []):
            if a["ok"]:
                txt.append("       + 第%d轮 %s -> 成功 %ss" % (a["round"], a["model"], a["seconds"]))
            else:
                txt.append("       - 第%d轮 %s -> 失败 HTTP=%s %ss [%s] %s" % (
                    a["round"], a["model"], a["http"], a["seconds"], a["error_code"], a["error_desc"]))
                if a.get("raw_message"):
                    txt.append("           接口原文：%s" % a["raw_message"])
        txt.append("")
    txt_path = os.path.join(out_dir, "result.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(txt))

    # ★新增(2026-11-06)：**追加式运行台账** `_runs.jsonl`（一行一张图，永不覆盖）。
    #   为什么加：`result.txt` / `result.json` 是**每次运行覆盖写**的 —— 分几批出图后，
    #   回头看"这张到底是哪个模型出的"就查不到了（用户 2026-11-06 就当场问了这个问题，
    #   当时只能靠对话记录里残留的字节数反推）。台账只追加、不删，事后可 grep 追溯。
    #   任何异常都不许影响交付 → 整段 try/except 包住。
    try:
        runs_path = os.path.join(out_dir, "_runs.jsonl")
        with open(runs_path, "a", encoding="utf-8") as f:
            for r in results:
                if not r:
                    continue
                f.write(json.dumps({
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "file": os.path.basename(r.get("file") or ""),
                    "ok": bool(r.get("ok")),
                    "model_used": r.get("model_used"),
                    "chain_head": r.get("chain_head") or (chain[0] if chain else None),
                    "fallback": bool(r.get("fallback")),
                    "seconds": r.get("seconds"),
                    "dims": r.get("dims"),
                    "bytes": r.get("bytes"),
                    "error_code": r.get("error_code"),
                    "prompt": (r.get("prompt") or "")[:200],
                }, ensure_ascii=False) + "\n")
    except Exception as e:
        print("（台账写入失败，不影响出图：%r）" % e)

    # ---- 比例校验：ratio 是否真的生效（实测会静默失效，2026-09-28 加）----
    want_ratio = (args.ratio or "").strip()
    if ok and want_ratio and want_ratio != "1:1":
        square = [r for r in ok if _is_square(r.get("dims"))]
        if square:
            print("\u26a0\ufe0f 比例告警：--ratio %s 没生效，%d/%d 张出的是正方形（%s）。"
                  % (want_ratio, len(square), len(ok), square[0].get("dims")))
            # ★改(2026-09-29)：主力换成 Qwen-Image-2.1 后，这段建议必须跟着改 ——
            #   它**不认 ratio，认 size**，照旧文案会把用户带偏（去换模型而不是加 --size）。
            # 主力模型不吃 size / ratio，恒出 1024×1024 —— 提示要按它的真实行为写。
            print("   ★ 主力模型 gpt-image-2.5-flare：size / ratio 都不吃，恒出 1024×1024。")
            print("     · 想要竖版/横版：先按方图出，再本地裁成目标比例")
            print("       （注意主体别顶边，否则会切到内容）。")
            print("   → 兜底办法：生成时把文字排窄（只占中间 3/4 宽）再本地裁，"
                  "或改用 HTML/SVG 排版（文字 100% 准、秒出、零额度）。")

    print("-" * 78)
    print("总计 %d 张：成功 %d，失败 %d，耗时 %ss" % (len(tasks), len(ok), len(bad), total))
    for r in ok:
        print("  出图：%s" % r["file"])
        # ★新增(2026-09-29)：**每张都打印可直接粘贴的 markdown 行** ——
        #   DSH **不会自动显示图片**，AI 不在正文写 `![]()` 用户就什么都看不到（全局规则 39）。
        #   实测教训（用户原话）：「你只是最终成品才给我看到，但你过程中的我也应该看到吧」
        #   —— 根因就是脚本只打印路径，AI 只在最后一次回复里贴了图。
        print_inline_markdown(r["file"], (r.get("prompt") or "生成结果").strip().split("\n")[0][:40])
    if bad:
        print("失败明细（去重）：")
        seen = set()
        for r in bad:
            key = (r.get("error_code"), r.get("error_desc"))
            if key in seen:
                continue
            seen.add(key)
            print("  [%s] %s" % (r.get("error_code"), r.get("error_desc")))
            for a in r.get("attempts", []):
                if not a["ok"] and a.get("raw_message"):
                    print("        接口原文（%s）：%s" % (a["model"], a["raw_message"]))
    print("结果文件：%s" % txt_path)
    print("机器可读：%s" % json_path)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not bad:
        return 0
    return 1 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
