#!/usr/bin/env python3
"""將所有 週報-*.md 轉成手機友善的靜態網站，輸出到 docs/（供 GitHub Pages 發佈）。

只用標準庫；針對本工具產生的 Markdown 子集（標題、清單、表格、連結、粗體、
分隔線）做轉換，不依賴任何第三方套件。
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = BASE_DIR / "docs"
REPORT_RE = re.compile(r"^週報-(\d{4}-\d{2}-\d{2})\.md$")

_LINK_RE = re.compile(r"\[((?:\\.|[^\]\\])*)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft JhengHei",
    "PingFang TC", "Noto Sans TC", sans-serif;
  line-height: 1.6; margin: 0; padding: 0;
  color: #1a1a1a; background: #f6f7f9;
}
.wrap { max-width: 960px; margin: 0 auto; padding: 16px; }
header.bar {
  position: sticky; top: 0; z-index: 10;
  background: #c4302b; color: #fff; padding: 12px 16px;
  display: flex; align-items: center; justify-content: space-between;
  box-shadow: 0 2px 6px rgba(0,0,0,.15);
}
header.bar a { color: #fff; text-decoration: none; font-weight: 600; }
header.bar .home { font-size: 14px; opacity: .9; }
h1 { font-size: 1.5rem; margin: .6em 0 .3em; }
h2 { font-size: 1.2rem; margin: 1.2em 0 .4em; padding-bottom: .2em;
     border-bottom: 2px solid #e3e3e3; }
h3 { font-size: 1.05rem; margin: 1em 0 .3em; color: #444; }
a { color: #1a5fb4; }
ul { padding-left: 1.2em; }
hr { border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }
.meta { color: #666; font-size: .9rem; }
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch;
  border: 1px solid #e3e3e3; border-radius: 8px; margin: .6em 0; }
table { border-collapse: collapse; width: 100%; font-size: .9rem;
  background: #fff; min-width: 640px; }
th, td { padding: 8px 10px; border-bottom: 1px solid #eee; text-align: left;
  white-space: nowrap; }
td:first-child, th:first-child { white-space: normal; min-width: 220px; }
thead th { background: #fafafa; position: sticky; top: 0; }
tbody tr:nth-child(even) { background: #fcfcfc; }
.cards { list-style: none; padding: 0; }
.cards li { margin: 0; }
.cards a { display: flex; align-items: center; justify-content: space-between;
  background: #fff; border: 1px solid #e3e3e3; border-radius: 10px;
  padding: 14px 16px; margin: 8px 0; text-decoration: none; color: #1a1a1a;
  box-shadow: 0 1px 3px rgba(0,0,0,.04); }
.cards a:hover { border-color: #c4302b; }
.cards .date { font-weight: 600; font-size: 1.05rem; }
.cards .tag { font-size: .8rem; color: #fff; background: #c4302b;
  border-radius: 999px; padding: 2px 10px; }
.cards .chev { color: #bbb; }
footer { color: #888; font-size: .8rem; text-align: center; padding: 24px 0; }
@media (prefers-color-scheme: dark) {
  body { color: #e6e6e6; background: #161718; }
  h2 { border-color: #333; } h3 { color: #bbb; }
  a { color: #6ab0ff; }
  .table-wrap, .cards a { border-color: #333; }
  table { background: #1e1f20; } th, td { border-color: #2a2b2c; }
  thead th { background: #242526; } tbody tr:nth-child(even) { background: #1b1c1d; }
  .cards a { background: #1e1f20; color: #e6e6e6; }
}
"""


def _unescape(text: str) -> str:
    # 還原 youtube_viral_radar.markdown_text() 加上的反斜線跳脫。
    return re.sub(r"\\(.)", r"\1", text)


def _render_plain(text: str) -> str:
    text = html.escape(_unescape(text))
    return _BOLD_RE.sub(r"<strong>\1</strong>", text)


def _render_inline(text: str) -> str:
    out: list[str] = []
    pos = 0
    for match in _LINK_RE.finditer(text):
        out.append(_render_plain(text[pos : match.start()]))
        label = _render_plain(match.group(1))
        url = html.escape(match.group(2), quote=True)
        out.append(f'<a href="{url}" target="_blank" rel="noopener">{label}</a>')
        pos = match.end()
    out.append(_render_plain(text[pos:]))
    return "".join(out)


def _split_row(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    # 以「未跳脫的 |」切欄，保留欄內的 \| 。
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row)]


def _alignments(separator_cells: list[str]) -> list[str]:
    aligns: list[str] = []
    for cell in separator_cells:
        cell = cell.strip()
        if cell.startswith(":") and cell.endswith(":"):
            aligns.append("center")
        elif cell.endswith(":"):
            aligns.append("right")
        else:
            aligns.append("")
    return aligns


def _render_table(table_lines: list[str]) -> str:
    rows = [_split_row(line) for line in table_lines]
    if len(rows) < 2:
        return "".join(f"<p>{_render_inline(line)}</p>" for line in table_lines)
    header, aligns, body = rows[0], _alignments(rows[1]), rows[2:]

    def cell_style(index: int) -> str:
        if index < len(aligns) and aligns[index]:
            return f' style="text-align:{aligns[index]}"'
        return ""

    parts = ['<div class="table-wrap"><table><thead><tr>']
    for index, cell in enumerate(header):
        parts.append(f"<th{cell_style(index)}>{_render_inline(cell)}</th>")
    parts.append("</tr></thead><tbody>")
    for row in body:
        parts.append("<tr>")
        for index, cell in enumerate(row):
            parts.append(f"<td{cell_style(index)}>{_render_inline(cell)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def markdown_to_html(md: str) -> str:
    lines = md.split("\n")
    parts: list[str] = []
    index, total = 0, len(lines)
    while index < total:
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if stripped == "---":
            parts.append("<hr>")
            index += 1
        elif stripped.startswith("### "):
            parts.append(f"<h3>{_render_inline(stripped[4:])}</h3>")
            index += 1
        elif stripped.startswith("## "):
            parts.append(f"<h2>{_render_inline(stripped[3:])}</h2>")
            index += 1
        elif stripped.startswith("# "):
            parts.append(f"<h1>{_render_inline(stripped[2:])}</h1>")
            index += 1
        elif stripped.startswith("- "):
            items = []
            while index < total and lines[index].strip().startswith("- "):
                items.append(f"<li>{_render_inline(lines[index].strip()[2:])}</li>")
                index += 1
            parts.append("<ul>" + "".join(items) + "</ul>")
        elif stripped.startswith("|"):
            table_lines = []
            while index < total and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            parts.append(_render_table(table_lines))
        else:
            parts.append(f"<p>{_render_inline(stripped)}</p>")
            index += 1
    return "\n".join(parts)


def _page(title: str, body: str, home_link: bool) -> str:
    home = (
        '<a class="home" href="index.html">← 所有週報</a>' if home_link else "<span></span>"
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-Hant">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{PAGE_CSS}</style>\n"
        "</head>\n<body>\n"
        f'<header class="bar"><a href="index.html">📈 爆款雷達</a>{home}</header>\n'
        f'<div class="wrap">\n{body}\n</div>\n'
        "</body>\n</html>\n"
    )


def build(base_dir: Path = BASE_DIR) -> int:
    docs = base_dir / "docs"
    docs.mkdir(exist_ok=True)
    (docs / ".nojekyll").write_text("", encoding="utf-8")

    reports: list[tuple[str, Path]] = []
    for path in base_dir.glob("週報-*.md"):
        match = REPORT_RE.match(path.name)
        if match:
            reports.append((match.group(1), path))
    reports.sort(key=lambda item: item[0], reverse=True)  # 最新在前

    for date_str, path in reports:
        body = markdown_to_html(path.read_text(encoding="utf-8"))
        page = _page(f"爆款雷達週報 {date_str}", body, home_link=True)
        (docs / f"report-{date_str}.html").write_text(page, encoding="utf-8")

    now = datetime.now(timezone.utc).astimezone()
    cards = ['<h1>YouTube 爆款雷達週報</h1>']
    cards.append(
        f'<p class="meta">共 {len(reports)} 份報表 · 最後更新 '
        f'{now:%Y-%m-%d %H:%M}（{now.tzname() or "本地"}）</p>'
    )
    if reports:
        cards.append('<ul class="cards">')
        for position, (date_str, _) in enumerate(reports):
            tag = '<span class="tag">最新</span>' if position == 0 else '<span></span>'
            cards.append(
                f'<li><a href="report-{date_str}.html">'
                f'<span class="date">{date_str}</span>{tag}'
                f'<span class="chev">›</span></a></li>'
            )
        cards.append("</ul>")
    else:
        cards.append("<p>目前還沒有任何週報。</p>")
    cards.append(
        '<footer>由 youtube_viral_radar 自動產生 · '
        'GitHub Pages 發佈</footer>'
    )
    index = _page("YouTube 爆款雷達週報", "\n".join(cards), home_link=False)
    (docs / "index.html").write_text(index, encoding="utf-8")

    print(f"已建站：{len(reports)} 份報表 → {docs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
