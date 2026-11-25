"""仓库里不许出现个人信息：跑在 CI 里的守卫，不依赖任何外部服务。

来源：AI 日报 2026-11-16 的两条——Lovable 默认权限泄露、香港迪士尼票务被盗。
这里的场景更朴素：解析出来的招标文件正文里有联系邮箱和手机号，
它们只该待在 data/（已忽略），一旦被误提交就永久留在历史里。
"""

import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 联系方式样式；故意写得宽，误报比漏报便宜
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

SKIP_DIRS = ("static/vendor/", "sample/", ".git/")
SKIP_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".woff2", ".min.js", ".min.css")
# 明确允许的样例值
ALLOW = {"caojianingof@163.com", "jningc@163.com"}


def tracked_files():
    out = subprocess.run(
        ["git", "ls-files"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for rel in out:
        if any(d in rel for d in SKIP_DIRS) or rel.endswith(SKIP_EXT):
            continue
        yield rel


def test_runtime_output_is_ignored():
    """data/step_output 必须被 .gitignore 覆盖，否则解析结果会被顺手提交。"""
    probe = "data/step_output/_probe.txt"
    r = subprocess.run(["git", "check-ignore", "-q", probe], cwd=PROJECT_ROOT)
    assert r.returncode == 0, "data/step_output 没被忽略，解析结果可能被提交"


def test_no_contact_info_in_tracked_files():
    hits = []
    for rel in tracked_files():
        path = PROJECT_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in EMAIL.finditer(text):
            if m.group(0).lower() not in ALLOW:
                hits.append(f"{rel}: 邮箱 {m.group(0)[:2]}***")
        for m in MOBILE.finditer(text):
            hits.append(f"{rel}: 手机号 {m.group(0)[:3]}****")
    assert not hits, "已跟踪文件里出现联系方式，移出仓库或加进 ALLOW：\n" + "\n".join(hits[:10])


if __name__ == "__main__":
    sys.exit(0 if (test_runtime_output_is_ignored() or True) and not False else 1)
