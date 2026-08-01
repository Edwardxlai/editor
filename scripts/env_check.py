"""环境预检 —— 换一台机器后第一个该跑的脚本。

回答一个问题：**在你这台机器上，这套流程现在能跑到哪一步。**

设计约束（决定了这个文件的写法）：
1. **零第三方依赖。** 它要在依赖装好之前就能跑，所以只用标准库。
2. **不 import pipeline。** `pipeline/config.py` 顶部有 Python 版本闸，
   版本不够时会 SystemExit —— 那样面试官看到的是一句裸报错，不是一份体检表。
   所以版本检查在本文件里自己做一遍，通过之后再谈别的。
3. **不调模型、不花钱。** 连通性只做 TLS 握手，不发请求。

用法：
    python scripts/env_check.py           # 体检
    python scripts/env_check.py --net     # 额外做一次 API 端点连通性握手

退出码 0 = 至少「不需要 key 的那条路」能走通。
"""

import os
import platform
import socket
import ssl
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

MIN_PYTHON = (3, 9)
TESTED_PYTHON = (3, 10, 11)

results: list = []          # (level, 项目, 结论) level: ok / warn / fail
sections: list = []         # (标题, 起始下标)


# ---------------------------------------------------------------------------
# 输出：先解决"这台终端能不能显示"，再显示别的
# ---------------------------------------------------------------------------

def setup_stdout() -> str:
    """返回最终可用的输出字符集描述。

    Windows 控制台默认 GBK、部分 CI 与 SSH 会话是 ASCII，
    直接 print 中文会抛 UnicodeEncodeError —— 那正是这个脚本要诊断的故障，
    它自己不能倒在同一个坑里。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)
        except (AttributeError, ValueError):
            pass
    enc = (getattr(sys.stdout, "encoding", "") or "ascii").lower()
    try:
        "✅ 中文".encode(enc)
        return "utf-8"
    except (UnicodeEncodeError, LookupError):
        return enc


CHARSET = setup_stdout()
RICH = CHARSET == "utf-8"
MARK = {"ok": "✅" if RICH else "[ OK ]",
        "warn": "⚠️ " if RICH else "[WARN]",
        "fail": "❌" if RICH else "[FAIL]"}


def say(text: str = "") -> None:
    if not RICH:
        text = text.encode(CHARSET, "replace").decode(CHARSET)
    print(text)


def add(level: str, name: str, detail: str = "") -> None:
    results.append((level, name, detail))


def section(title: str) -> None:
    sections.append((title, len(results)))


# ---------------------------------------------------------------------------
# 1. 解释器
# ---------------------------------------------------------------------------

def check_python() -> bool:
    section("解释器")
    v = sys.version_info
    cur = f"{v.major}.{v.minor}.{v.micro}"
    if v < MIN_PYTHON:
        add("fail", f"Python {cur}",
            f"低于下限 {MIN_PYTHON[0]}.{MIN_PYTHON[1]}，无法运行。"
            f"macOS 装新版：brew install python@3.12")
        return False
    if (v.major, v.minor) < TESTED_PYTHON[:2]:
        add("warn", f"Python {cur}",
            f"高于下限但低于实测版本 {'.'.join(map(str, TESTED_PYTHON))}，"
            f"预期可用，未逐项验证")
    else:
        add("ok", f"Python {cur}", platform.python_implementation())

    add("ok", "解释器路径", sys.executable)
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    add("ok" if in_venv else "warn",
        "虚拟环境",
        "已在 venv 内" if in_venv else
        "不在 venv 内。Homebrew / Debian 的 Python 会拒绝 pip 安装"
        "（externally-managed-environment），届时按 README 建 venv")
    return True


# ---------------------------------------------------------------------------
# 2. 平台
# ---------------------------------------------------------------------------

def check_platform() -> None:
    section("平台")
    add("ok", "操作系统", f"{platform.system()} {platform.release()} / {platform.machine()}")
    add("ok" if RICH else "warn", "控制台字符集",
        "UTF-8，中文与符号正常" if RICH else
        f"{CHARSET} —— 中文可能显示为 ?，不影响文件内容（文件一律 UTF-8 写入）")

    # 文件系统大小写敏感性：macOS 默认不敏感，Linux 与部分 APFS 卷敏感。
    # 敏感的机器上任何路径大小写笔误都会变成 FileNotFoundError。
    with tempfile.TemporaryDirectory(dir=str(ROOT)) as td:
        probe = Path(td) / "CaseProbe.tmp"
        probe.write_text("x", encoding="utf-8")
        sensitive = not (Path(td) / "caseprobe.tmp").exists()
    add("ok", "文件系统", "区分大小写" if sensitive else "不区分大小写")

    # 项目路径含中文。跨平台压缩包解压时可能乱码，
    # 但代码引用的路径全是 ASCII —— 乱码只影响文档链接，不影响运行。
    non_ascii = not str(ROOT).isascii()
    add("ok", "项目路径",
        f"{ROOT}" + ("（含非 ASCII 字符；代码内部路径全为 ASCII，不受影响）"
                     if non_ascii else ""))


# ---------------------------------------------------------------------------
# 3. 依赖
# ---------------------------------------------------------------------------

def check_deps() -> None:
    section("依赖（只有两个）")
    for mod, pkg, needed_for in [
        ("httpx", "httpx", "调用模型（run.py / ingest.py 的联网部分）"),
        ("docx", "python-docx", "解析 .docx 样稿（缺失时自动走 zipfile 兜底）"),
    ]:
        try:
            m = __import__(mod)
            add("ok", pkg, f"{getattr(m, '__version__', '版本未知')}")
        except ImportError:
            level = "warn" if mod == "docx" else "warn"
            add(level, pkg, f"未安装 —— 影响：{needed_for}")


# ---------------------------------------------------------------------------
# 4. 文件
# ---------------------------------------------------------------------------

def probe_text(path: Path) -> str:
    """返回这份文本文件的编码/行尾诊断。"""
    raw = path.read_bytes()
    notes = []
    if raw.startswith(b"\xef\xbb\xbf"):
        notes.append("含 BOM（已按 utf-8-sig 兼容读取）")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "❗ 不是合法 UTF-8 —— 很可能在传输中被按 GBK 重存过"
    if b"\r\n" in raw:
        notes.append("CRLF 行尾")
    return "、".join(notes) if notes else "UTF-8 / LF"


def check_files() -> None:
    section("输入文件（题目方提供，缺一不可）")
    inputs = [
        ("data/interview_transcript.srt", True),
        ("data/content_brief.md", True),
        ("data/historical_notes.jsonl", True),
        ("data/published_samples", True),
    ]
    for rel, required in inputs:
        p = ROOT / rel
        if not p.exists():
            add("fail" if required else "warn", rel, "缺失")
            continue
        if p.is_dir():
            n = len(list(p.glob("*.docx")))
            add("ok" if n else "fail", rel, f"{n} 个 .docx")
        else:
            add("ok", rel, f"{p.stat().st_size:,} 字节 / {probe_text(p)}")

    section("离线产物（ingest.py 已跑过就应存在）")
    for rel in ["store/segments.jsonl", "store/notes_meta.jsonl"]:
        p = ROOT / rel
        if not p.exists():
            add("warn", rel, "缺失 —— run.py 的历史复用判断会降级，先跑 python ingest.py")
            continue
        n = sum(1 for line in p.read_text(encoding="utf-8-sig").splitlines() if line.strip())
        add("ok", rel, f"{n} 条")

    section("prompt 模板")
    bad = []
    for name in ["extract", "reuse", "angles", "outline", "draft", "segment"]:
        p = ROOT / "prompts" / f"{name}.md"
        if not p.exists():
            bad.append(f"{name}.md 缺失")
        elif "@@@USER@@@" not in p.read_text(encoding="utf-8-sig"):
            bad.append(f"{name}.md 缺分隔符")
    add("fail" if bad else "ok", "6 个模板可解析", "；".join(bad) if bad else "全部就绪")

    section("交付物")
    outs = ["angles.md", "outline.md", "evidence.md",
            "reuse_suggestions.md", "draft_v0_5.md", "editorial_checks.md"]
    have = [f for f in outs if (ROOT / "outputs" / f).exists()
            and (ROOT / "outputs" / f).stat().st_size > 0]
    add("ok" if len(have) == 6 else "warn", "outputs/ 六份",
        f"{len(have)}/6" + ("" if len(have) == 6 else " —— 跑 python run.py 生成"))

    section("写权限")
    for rel in ["outputs", "store"]:
        d = ROOT / rel
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_probe.tmp"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            add("ok", f"{rel}/ 可写", "")
        except OSError as exc:
            add("fail", f"{rel}/ 不可写", str(exc))


# ---------------------------------------------------------------------------
# 5. 配置
# ---------------------------------------------------------------------------

def read_dotenv() -> dict:
    """与 pipeline/config.load_dotenv 同口径，但不 import 它（见模块 docstring）。"""
    path = ROOT / ".env"
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def check_config(do_net: bool) -> None:
    section("模型配置（只影响需要 key 的那条路）")
    if not (ROOT / ".env").exists():
        add("warn", ".env", "不存在 —— 不影响 selftest / check / --dry-run；"
                            "要真跑 run.py 就照 .env.example 建一个")
        return

    raw = (ROOT / ".env").read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        add("ok", ".env 含 BOM", "已按 utf-8-sig 读取，不会误伤首行的键")

    env = read_dotenv()
    provider = (env.get("PROVIDER") or os.environ.get("PROVIDER") or "openai").lower()
    prefix = "ANTHROPIC" if provider == "anthropic" else "OPENAI"
    key = env.get(f"{prefix}_API_KEY") or os.environ.get(f"{prefix}_API_KEY", "")
    model = env.get(f"{prefix}_MODEL") or os.environ.get(f"{prefix}_MODEL", "")
    base = (env.get(f"{prefix}_BASE_URL")
            or os.environ.get(f"{prefix}_BASE_URL")
            or ("https://api.anthropic.com" if provider == "anthropic"
                else "https://api.openai.com/v1"))

    add("ok" if provider in ("openai", "anthropic") else "fail",
        "PROVIDER", provider)
    add("ok" if key else "warn", f"{prefix}_API_KEY",
        f"已填（{key[:6]}…{key[-4:]}，{len(key)} 位）" if key else "未填")
    add("ok" if model else "warn", f"{prefix}_MODEL", model or "未填")
    add("ok", f"{prefix}_BASE_URL", base)

    mt = env.get("MAX_TOKENS") or os.environ.get("MAX_TOKENS", "")
    if mt.isdigit() and int(mt) < 16000:
        add("warn", "MAX_TOKENS", f"{mt} —— 推理模型的思考链算在里面，"
                                  f"实测 8000 会让正文返回空。建议 32000（README §三）")
    elif mt:
        add("ok", "MAX_TOKENS", mt)

    if do_net:
        host = urlparse(base).hostname or ""
        port = urlparse(base).port or 443
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=8) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    add("ok", f"连通性 {host}:{port}",
                        f"TLS 握手成功（{tls.version()}）；未发送任何请求，不计费")
        except Exception as exc:
            add("warn", f"连通性 {host}:{port}",
                f"{type(exc).__name__}: {exc} —— 代理/网络受限时 run.py 会超时")


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def verdict() -> int:
    fails = [r for r in results if r[0] == "fail"]
    warns = [r for r in results if r[0] == "warn"]

    say()
    say("=" * 62)
    if fails:
        say(f"{MARK['fail']} 有 {len(fails)} 项硬性缺失，先修这些：")
        for _, name, detail in fails:
            say(f"    · {name}：{detail}")
        return 1

    # 不需要 key 的那条路是否走得通 —— 这才是"能不能验收"的判据
    dep_missing = {name for level, name, _ in results
                   if level == "warn" and name in ("httpx", "python-docx")}
    say(f"{MARK['ok']} 不需要 API key 的这三条命令现在就能跑：")
    say("      python scripts/selftest.py     确定性层自检 27 项")
    say("      python check.py                验收清单 12 项（读现成的 outputs/）")
    say("      python run.py --dry-run        导出 prompt 供审查")
    if "httpx" in dep_missing:
        say()
        say(f"{MARK['warn']} httpx 未安装 —— 要真跑 run.py / ingest.py 需先："
            f"\n      pip install -r requirements.txt")
    if warns:
        say()
        say(f"{MARK['warn']} {len(warns)} 项提示（不阻塞上面三条命令），见上。")
    say("=" * 62)
    return 0


def main() -> int:
    say(f"环境预检 —— {ROOT.name}")
    say("=" * 62)

    if not check_python():
        for level, name, detail in results:
            say(f"{MARK[level]} {name:<28} {detail}")
        return 1

    check_platform()
    check_deps()
    check_files()
    check_config("--net" in sys.argv)

    bounds = [start for _, start in sections] + [len(results)]
    for (title, start), end in zip(sections, bounds[1:]):
        say()
        # 中文按两个终端列宽算，减号补到固定总宽，各节标题才对得齐
        width = sum(2 if ord(ch) > 0x2000 else 1 for ch in title)
        say(f"— {title} " + "-" * max(4, 58 - width))
        for level, name, detail in results[start:end]:
            say(f"{MARK[level]} {name:<26} {detail}")

    return verdict()


if __name__ == "__main__":
    sys.exit(main())
