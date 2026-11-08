# 生图 skill

一句话：**装好之后，跟 AI 说一句「帮我画一只橘猫」，它就把图给你画出来。**

不用懂代码，不用记参数。装一次，以后一直能用。

---

## 一、装（一条命令）

### Windows

按 `Win + R`，输入 `powershell`，回车，然后把下面整行粘进去（把 `sk-` 那串换成你的 key）：

```powershell
irm https://ghproxy.net/https://raw.githubusercontent.com/xjh880000/gen-image-skill/main/install.ps1 -OutFile "$env:TEMP\gi.ps1"; powershell -ExecutionPolicy Bypass -File "$env:TEMP\gi.ps1" "sk-你的key"
```

> 上面这条走的是国内加速通道（ghproxy）。万一它也抽风，把开头的
> `https://ghproxy.net/https://raw.githubusercontent.com/` 换成
> `https://cdn.jsdelivr.net/gh/xjh880000/gen-image-skill@main/` 再试一次。

### Mac / Linux

打开「终端」，把下面整行粘进去：

```bash
curl -fsSL https://ghproxy.net/https://raw.githubusercontent.com/xjh880000/gen-image-skill/main/install.sh | bash -s -- "sk-你的key"
```

> 这条不通就把 `https://ghproxy.net/https://raw.githubusercontent.com/` 换成
> `https://cdn.jsdelivr.net/gh/xjh880000/gen-image-skill@main/`。

看到 `装完了` 就是好了。整个过程大概 30 秒。

---

## 二、用

### 方式 1：跟 AI 说（推荐）

装好之后，打开你的 AI 客户端（Claude Code / DSH 之类），直接说：

```
帮我画一张图：一只橘猫坐在窗台，午后暖光
```

或者更具体一点，效果更好：

```
帮我画一张图：一只橘猫坐在木窗台上，午后阳光从左边照进来，
背景有绿植和窗帘，写实照片风格，暖色调
```

**不会写描述也没关系**，就说你想要的画面是什么样，越具体越好。可以只说：

- 「画个头像，卡通风格，一只柴犬」
- 「画个壁纸，深蓝色夜空，有星星和山」
- 「把这张照片背景换成海边」（这个要先发图片给它）

### 方式 2：直接命令行（不用 AI）

```bash
python gen-image.py -p "一只橘猫坐在窗台，午后暖光"
```

图会存在当前目录的 `gen-image-out/` 文件夹里。

---

## 三、出图要多久、花多少额度

- **一张图大约 30 秒**（默认模型 `gpt-image-2.5-flare`）
- 每次出图会消耗你账号里的额度
- 主力模型挂了会自动换下一个，你不用管

---

## 四、想改配置

打开这个文件（用记事本就行）：

```
~/.claude/skills/gen-image/config.json
```

（Windows 上就是 `C:\Users\你的用户名\.claude\skills\gen-image\config.json`）

里面长这样：

```json
{
  "base": "https://xyuapi.top/v1",
  "key": "sk-你的key",
  "model": "gpt-image-2.5-flare",
  "chain": ["gpt-image-2.5-flare", "NanoBanana-Pro", "gpt-image-2-pro", "gpt-image-2.5-sunburst"]
}
```

**一般只改 `key`。** 想换模型就改 `model`，可选的有：

| 模型 | 出图速度 | 说明 |
|---|---|---|
| `gpt-image-2.5-flare` | 约 30 秒 | 默认，最快也最好看 |
| `gpt-image-2-pro` | 约 46 秒 | 备选 |
| `NanoBanana-Pro` | 约 81 秒 | 慢，但构图稳 |
| `gpt-image-2.5-sunburst` | 约 30 秒 | 同系列，风格略不同 |

---

## 五、出问题了

### 「没找到 Python」

去 https://www.python.org/downloads/ 下载安装，**安装时一定要勾上 `Add Python to PATH`**，装完重开终端再跑一次安装命令。

### 「自检没通过」/「无效的令牌」

说明 key 不对或者过期了。检查 `config.json` 里的 `key` 是不是完整（`sk-` 开头，后面 48 位）。

### 图出不来，但没报错

手动跑这条，能看到具体原因：

```bash
python ~/.claude/skills/gen-image/scripts/gen_image.py -p "test" 
```

### 想看这个 key 能用哪些模型

```bash
python ~/.claude/skills/gen-image/scripts/gen_image.py --list-models
```

---

## 六、这个包里有什么

```
gen-image-skill/
├── README.md               ← 你正在看的这个
├── SKILL.md                ← AI 读的说明书（不用管）
├── config.json             ← 你的配置（key 在这里）
├── install.sh / install.ps1← 安装脚本
├── requirements.txt        ← 依赖清单
└── scripts/
    ├── gen_image.py        ← 真正干活的程序
    ├── version_store.py    ← 记录每次生成的版本
    └── make_selfcontained.py ← 把多张图打包成一个 HTML
```
