# AI 增强内容复盘工具

笔记侠 FDE 加速营 · 第二轮实战题目一 · 最小可运行原型

输入一场访谈的逐字稿 + 历史笔记库,输出一套**证据锚定的编辑工作包**:6 份 Markdown,每一条判断都能回指到具体的字幕号、时间戳和说话人;每一条无出处的事实性说法都已被标出;每一条历史素材的复用与否都附判断理由。

---

## 四份产出在哪

| 题目产出 | 权重 | 文件 |
|---|---:|---|
| 产出 1　需求澄清与问题定义 | 10% | [`分析/产出1_需求澄清.md`](分析/产出1_需求澄清.md) |
| 产出 2　方案设计 | 30% | [`PRD.md`](PRD.md) —— 流程、人机分工、质量 Rubric、异常处理 |
| 产出 3　最小可运行原型 | 40% | 本仓库代码,运行方式见下文 |
| 产出 4　5 分钟讲解视频 | 20% | 讲解脚本见 [`分析/产出4_讲解视频脚本.md`](分析/产出4_讲解视频脚本.md) |

`分析/` 下其余文件是设计过程记录,不属于四份产出,供追问时查证判断依据。

---

## 一、运行环境

| 项 | 要求 |
|---|---|
| Python | 3.9 起可用(低于此版本启动即报错);**实测 3.10.11** |
| 依赖 | 只有 `httpx`、`python-docx` 两个 |
| 系统 | 无平台相关代码:路径全走 `pathlib`,读写一律显式 UTF-8,无 shell 调用 |

**验收不需要装依赖,也不需要 API key。** 下面两条命令在一台只有 Python 的裸机器上直接可跑:

```bash
python check.py                # 验收清单 12 项,读现成的 outputs/
python run.py --dry-run        # 导出 prompt 供审查,不调模型
```

要**重新生成** `outputs/`(即真的调模型)才需要装依赖并配 key:

```bash
# macOS / Linux
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Windows PowerShell
py -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> Homebrew / Debian 的系统 Python 会拒绝直接 `pip install`(`error: externally-managed-environment`)—— 建 venv 是最省事的绕法。
> `python-docx` 缺失不阻塞:`pipeline/docx_read.py` 有 zipfile 兜底。

## 二、配置

```bash
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
```

变量名用 `OPENAI_` / `ANTHROPIC_` 而不是厂商专名 —— 任何兼容这两种协议的服务都能直接跑。

```ini
PROVIDER=openai
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_API_KEY=sk-...
OPENAI_MODEL=deepseek-v4-flash
MAX_TOKENS=32000
```

> ⚠️ **用推理模型时 `MAX_TOKENS` 必须调高。** DeepSeek-V4、o 系列等推理模型的 `max_tokens` **包含思考链**。实测单次入库调用消耗 23,376 输出 token,其中 22,501 是思考 —— 设成 8000 会让思考链吃光预算,正文返回空字符串。代码会识别这种情况并直接报错给出修复指引,**不会重试**(这是配置问题,重试只是重复烧钱)。
>
> ⚠️ `.env` 含真实 key,已列入 `.gitignore`。但**打包 zip 时 `.gitignore` 不生效** —— 交付前确认包里只有 `.env.example`。

## 三、启动方式

```bash
python ingest.py       # 一次性:历史笔记入库 → 历史笔记段表
python run.py          # 主流程:产出 6 个交付文件
python check.py        # 验收清单
```

`ingest.py` 是**离线一次性**的:历史笔记是静态的,抽一次永久复用,不必每次跑主流程时重读(既省 token,也避免约束在不同运行间漂移)。

常用参数:

| 命令 | 作用 |
|---|---|
| `python run.py --dry-run` | 只导出 prompt 不调模型,**不需要 API key** |
| `python run.py --cache` | 复用已有中间产物,只跑缺的阶段 |
| `python run.py --until extract` | 跑到指定阶段为止 |
| `python run.py --angle 2` | 用第 2 个角度写初稿(默认第 1 个) |
| `python ingest.py --only note_03` | 只入库指定笔记(增量合并) |

> `scripts/` 下另有几个开发期辅助脚本(环境体检、确定性层自检、输出快照对比、样稿解析),验收不需要跑,说明见各文件头部注释。

## 四、输入文件

| 路径 | 说明 |
|---|---|
| `data/interview_transcript.srt` | 访谈逐字稿,**证据溯源的基准输入** |
| `data/content_brief.md` | 本次内容任务背景、目标读者、时效要求 |
| `data/historical_notes.jsonl` | 8 篇脱敏历史商业笔记 |
| `data/published_samples/*.docx` | 3 篇脱敏已发布样稿,用于归纳风格 |

## 五、输出位置

```
outputs/
├── angles.md              3 个内容标题 / 切入角度备选
├── outline.md             文章结构与关键要点(两级标题树)
├── evidence.md            关键观点对应的原始素材证据 ★ 主表
├── reuse_suggestions.md   历史内容复用建议 / 为什么不应复用
├── draft_v0_5.md          可编辑初稿,含内联证据标记
├── editorial_checks.md    需编辑人工确认的事实、风险与缺失信息
└── _intermediate/         各阶段中间产物 JSON(可单步重跑、可离线校验)

store/
├── segments.jsonl         历史笔记段表(ingest.py 产出)
├── notes_meta.jsonl       历史笔记篇级元数据
└── style_card.md          风格卡(人定稿,结论已写进 prompts/)
```

**★ `evidence.md` 是主表,其余五个文件都是它的视图。** 抽取只跑一次,下游只做筛选与引用,不重新抽取 —— 证据溯源因此天然成立。

六个文件是编辑动线上的一条链,**每份只做自己那一环,不预演下一环**:

```
angles 选  →  outline 搭  →  evidence 查  →  draft 写  →  reuse 补  →  checks 核
```

所以 outline 里不摆原话(那是 evidence 的活),angles 里不写选题方法论(那是 PRD 的活),checks 里不列时间戳重叠(编辑不会因此改任何一个字)。**`outputs/` 只放编辑当天要用的东西。**

> 设计理由(为什么这样分工、为什么不用 RAG、质量 Rubric 怎么定)全部在 [`PRD.md`](PRD.md),此处不重复。

## 六、第三方代码、模型与素材标注

| 类别 | 使用内容 |
|---|---|
| 第三方库 | `httpx`(HTTP 客户端)、`python-docx`(读 .docx) |
| 模型 | 通过 OpenAI 兼容协议调用;默认配置为 DeepSeek `deepseek-v4-flash`。代码不绑厂商,换 `.env` 即可切换 |
| 公开素材 | 无。未使用任何外部数据集、预训练权重或第三方语料 |
| 生成代码 | 本仓库代码在 AI 辅助下编写,全部逻辑经人工审阅与实测验证 |

材料包内容(逐字稿、历史笔记、样稿)来自题目方,已脱敏,仅用于本次面试题。本工具**不做反向识别**,且禁止把历史笔记中的机构名/网址带入本次交付物 —— 两版素材脱敏口径不一致,存在回流风险。

## 七、跨平台情况

不知道拿到这个包的机器是什么,所以把"换一台机器会坏在哪"当成一类故障来测:

- **平台相关代码为零** —— 全仓库无 `os.name` / `sys.platform` / `subprocess` / 硬编码盘符;路径全部由 `__file__` 推出
- **零依赖可跑** —— 屏蔽 `httpx` + `python-docx` 后,`check.py` 全绿、`run.py --dry-run` 正常导出(样稿解析自动降级到 zipfile 兜底)
- **编码** —— `.env` 按 `utf-8-sig` 读(带 BOM 不会误判为缺 key);SRT 按 `\r?\n` 切块,CRLF / LF 等价;各入口显式切 UTF-8 输出

**仍未覆盖**:只在 Windows / Python 3.10.11 上实机跑过完整流程。Linux、macOS 与 3.9 是静态与模拟验证(语法编译、依赖屏蔽、编码注入),不是真机实测。

## 八、已知限制

| 限制 | 说明 |
|---|---|
| 未定阈值 | 两项未定案:evidence 要点数量(占位 `8–16`)、单篇历史笔记段上限(占位 `10`),都写在 `pipeline/config.py:TBD_PLACEHOLDERS`。它们要的是业务校准(编辑实际用得上几条),不是再跑一次实验 |
| 不做事实核实 | 无联网、无权威源。工具只标出"什么需要核实",核实由编辑做 |
| 不做读者匹配自动判定 | `适用读者` 是自然语言,编码需先定义标签体系,目前无依据。原文列出该字段 + 标"需编辑确认" |
| 单篇处理 | 未做多篇批处理 |
| 无原始视频 | 部分指代(如"你的例子是护士录笔记")无法消歧,已列入 `editorial_checks.md` |
| 无出处不得进结构 | 没有硬约束。本次运行里四条无出处要点恰好都没进结构,但那是结果不是机制保证 |
