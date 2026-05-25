#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate CSDN expert-annotation drafts for an article.

The tool only prepares annotation suggestions. It does not submit anything to
CSDN, so the final choice and submission stay under human review.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import ssl
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable, List, Optional


ANNOTATION_TYPES = (
    "运行环境 & 效果",
    "适用场景",
    "补充案例",
    "内容质量",
)


@dataclass
class Article:
    title: str
    paragraphs: List[str]


@dataclass
class AnnotationDraft:
    annotation_type: str
    selected_text: str
    content: str
    reason: str


class TextBlockParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._current_tag: Optional[str] = None
        self._buffer: List[str] = []
        self.blocks: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in {"p", "li", "h1", "h2", "h3", "h4", "pre", "blockquote"}:
            self._flush()
            self._current_tag = tag
        elif tag == "br":
            self._buffer.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in {"p", "li", "h1", "h2", "h3", "h4", "pre", "blockquote"}:
            self._flush()
            self._current_tag = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if data and self._current_tag:
            self._buffer.append(data)

    def _flush(self) -> None:
        text = normalize_text("".join(self._buffer))
        self._buffer = []
        if len(text) >= 12:
            self.blocks.append(text)


def normalize_text(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\n{2,}", "\n", value)
    return value.strip()


def fetch_html(url: str, timeout: int = 20, insecure: bool = False) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    context = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        data = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return data.decode(charset, errors="replace")


def read_html(args: argparse.Namespace) -> str:
    if args.html_file:
        with open(args.html_file, "r", encoding=args.encoding, errors="replace") as fp:
            return fp.read()
    if not args.url:
        raise SystemExit("请传入 CSDN 文章 URL，或使用 --html-file 指定本地 HTML 文件。")
    try:
        return fetch_html(args.url, args.timeout, args.insecure)
    except urllib.error.URLError as exc:
        raise SystemExit(
            "抓取网页失败。可以先在浏览器中打开文章并另存为 HTML，再用 "
            f"--html-file 分析。\n错误: {exc}"
        ) from exc


def extract_article_html(page_html: str) -> str:
    patterns = (
        r'<article\b[^>]*class="[^"]*baidu_pl[^"]*"[^>]*>(?P<body>.*?)</article>',
        r'<div\b[^>]*id="content_views"[^>]*>(?P<body>.*?)</div>',
        r'<div\b[^>]*class="[^"]*article_content[^"]*"[^>]*>(?P<body>.*?)</div>',
    )
    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group("body")
    return page_html


def extract_title(page_html: str, paragraphs: List[str]) -> str:
    title_patterns = (
        r"<h1[^>]*>(?P<title>.*?)</h1>",
        r"<title[^>]*>(?P<title>.*?)</title>",
    )
    for pattern in title_patterns:
        match = re.search(pattern, page_html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = normalize_text(re.sub(r"<[^>]+>", "", match.group("title")))
            title = re.sub(r"[_-]CSDN博客.*$", "", title).strip()
            if title:
                return title
    return paragraphs[0][:80] if paragraphs else "未识别标题"


def parse_article(page_html: str) -> Article:
    body = extract_article_html(page_html)
    parser = TextBlockParser()
    parser.feed(body)
    paragraphs = dedupe_blocks(parser.blocks)
    if not paragraphs and page_html.strip():
        return parse_plain_text_article(page_html)
    title = extract_title(page_html, paragraphs)
    return Article(title=title, paragraphs=paragraphs)


def parse_plain_text_article(text: str) -> Article:
    lines = [normalize_text(line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return Article(title="未识别标题", paragraphs=[])

    title = lines[0][:80]
    blocks = re.split(r"\n\s*\n+", text.replace("\r\n", "\n").replace("\r", "\n"))
    paragraphs = dedupe_blocks(blocks)
    if len(paragraphs) <= 1:
        paragraphs = dedupe_blocks(lines)
    if paragraphs and normalize_text(paragraphs[0]) == normalize_text(title):
        paragraphs = paragraphs[1:] or paragraphs
    return Article(title=title, paragraphs=paragraphs)


def dedupe_blocks(blocks: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for block in blocks:
        block = normalize_text(block)
        if len(block) < 12:
            continue
        key = block[:120]
        if key in seen:
            continue
        seen.add(key)
        result.append(block)
    return result


def classify(block: str) -> tuple[str, int, str]:
    lower = block.lower()
    score = 0
    if re.search(r"python|java|spring|node|npm|pip|maven|版本|环境|安装|运行|依赖|命令|代码|配置|api|sdk", lower):
        return "运行环境 & 效果", 90, "包含环境、依赖、配置或代码运行信息"
    if re.search(r"适合|适用|应用|场景|业务|企业|项目|系统|领域|报表|可视化|生产|上线", block):
        return "适用场景", 80, "包含适用场景或业务落地描述"
    if re.search(r"案例|实践|示例|实战|经验|踩坑|效果|优化|方案", block):
        return "补充案例", 70, "适合补充实践经验或案例说明"
    if re.search(r"必须|一定|最佳|最新|全面|完整|简单|只需|原理|总结|注意|缺点|优点", block):
        return "内容质量", 75, "包含结论性或质量判断信息"
    return "内容质量", 45, "适合补充准确性、完整性或可复现性评价"


def make_selected_text(block: str) -> str:
    text = normalize_text(block)
    if len(text) <= 500:
        return text
    return text[:497].rstrip() + "..."


def make_annotation(annotation_type: str, block: str, title: str) -> str:
    if annotation_type == "运行环境 & 效果":
        return (
            "建议补充精确运行环境、依赖版本和实际运行结果。若涉及代码或配置，最好说明测试平台、"
            "关键版本及是否可复现，方便读者判断当前内容的参考价值。"
        )
    if annotation_type == "适用场景":
        return (
            "这段适合作为场景说明，但建议进一步写清适用边界。可补充适合的业务规模、数据量、"
            "团队能力要求，以及不建议采用该方案的典型情况。"
        )
    if annotation_type == "补充案例":
        return (
            "可以补充一个真实项目案例，包括使用背景、关键实现步骤和最终效果。这样能帮助读者从"
            "概念说明过渡到落地实践，也更容易评估方案成本。"
        )
    return (
        "这段内容具有参考价值，但建议补充来源依据、版本条件或边界说明。若结论来自特定环境，"
        "应明确前提，避免读者在不同场景下直接套用。"
    )


def build_drafts(article: Article, limit: int) -> List[AnnotationDraft]:
    scored = []
    for index, block in enumerate(article.paragraphs):
        if is_noise(block):
            continue
        annotation_type, score, reason = classify(block)
        score += min(len(block), 500) // 20
        scored.append((score, index, annotation_type, block, reason))

    scored.sort(key=lambda item: (-item[0], item[1]))
    drafts: List[AnnotationDraft] = []
    used_types = set()
    used_text_keys = set()

    for _, _, annotation_type, block, reason in scored:
        key = normalize_text(block)[:80]
        if key in used_text_keys:
            continue
        if len(drafts) >= limit:
            break
        if annotation_type in used_types and len(used_types) < min(limit, len(ANNOTATION_TYPES)):
            continue
        used_types.add(annotation_type)
        used_text_keys.add(key)
        drafts.append(
            AnnotationDraft(
                annotation_type=annotation_type,
                selected_text=make_selected_text(block),
                content=make_annotation(annotation_type, block, article.title),
                reason=reason,
            )
        )

    for _, _, annotation_type, block, reason in scored:
        if len(drafts) >= limit:
            break
        key = normalize_text(block)[:80]
        if key in used_text_keys:
            continue
        used_text_keys.add(key)
        drafts.append(
            AnnotationDraft(
                annotation_type=annotation_type,
                selected_text=make_selected_text(block),
                content=make_annotation(annotation_type, block, article.title),
                reason=reason,
            )
        )
    return drafts


def is_noise(block: str) -> bool:
    noise_patterns = (
        "点赞",
        "评论",
        "收藏",
        "复制链接",
        "举报",
        "阅读全文",
        "CSDN",
        "版权声明",
    )
    if len(block) < 20:
        return True
    if any(pattern in block and len(block) < 80 for pattern in noise_patterns):
        return True
    return False


def print_text(article: Article, drafts: List[AnnotationDraft], url: Optional[str]) -> None:
    print(f"文章标题：{article.title}")
    if url:
        print(f"文章链接：{url}")
    print(f"标注草稿数量：{len(drafts)}")
    print()
    for index, draft in enumerate(drafts, 1):
        print(f"--- 标注 {index} ---")
        print(f"类型：{draft.annotation_type}")
        print(f"推荐原因：{draft.reason}")
        print("建议选中文本：")
        print(textwrap.fill(draft.selected_text, width=88))
        print("标注内容：")
        print(textwrap.fill(draft.content, width=88))
        print()


def print_json(article: Article, drafts: List[AnnotationDraft], url: Optional[str]) -> None:
    payload = {
        "title": article.title,
        "url": url,
        "annotations": [
            {
                "type": draft.annotation_type,
                "selectedText": draft.selected_text,
                "content": draft.content,
                "reason": draft.reason,
            }
            for draft in drafts
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate CSDN expert-annotation drafts for manual review."
    )
    parser.add_argument("url", nargs="?", help="CSDN article URL.")
    parser.add_argument("--html-file", help="Analyze a saved local HTML file instead of fetching the URL.")
    parser.add_argument("--encoding", default="utf-8", help="Encoding for --html-file. Default: utf-8.")
    parser.add_argument("--timeout", type=int, default=20, help="Network timeout in seconds. Default: 20.")
    parser.add_argument("--insecure", action="store_true", help="Skip HTTPS certificate verification.")
    parser.add_argument("--max", type=int, default=5, help="Maximum drafts to output. Default: 5.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    limit = max(1, min(args.max, 5))
    page_html = read_html(args)
    article = parse_article(page_html)
    drafts = build_drafts(article, limit)
    if args.format == "json":
        print_json(article, drafts, args.url)
    else:
        print_text(article, drafts, args.url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
