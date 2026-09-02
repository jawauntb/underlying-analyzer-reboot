"""Render an analyst memo as a styled PDF using ReportLab.

This module produces a polished, terminal-aesthetic PDF of an analyst Vision
Memo. It is intentionally tolerant of missing optional fields — sections that
have no data are skipped entirely, and the top-level entry point
:func:`render_memo_pdf` never raises: on catastrophic failure it returns a
minimal "error" PDF instead.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    Image,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Color palette — mirrors app/charts.py and the front-end terminal aesthetic.
# ---------------------------------------------------------------------------

PAGE_BG = colors.HexColor("#10151b")
PANEL_BG = colors.HexColor("#05070a")
PANEL_BG_SOFT = colors.HexColor("#0d171d")
SCANLINE = colors.Color(1, 1, 1, alpha=0.025)

AMBER = colors.HexColor("#ffc94a")
AMBER_HOT = colors.HexColor("#ffe66f")
CYAN = colors.HexColor("#57d9ff")
GREEN = colors.HexColor("#79ff9c")
RED = colors.HexColor("#ff695d")
VIOLET = colors.HexColor("#b28cff")
MUTED = colors.HexColor("#5a6470")
BODY = colors.HexColor("#d4dde5")
STRONG = colors.HexColor("#ffffff")
RULE = colors.HexColor("#1f2731")

# Margins / page geometry.
PAGE_W, PAGE_H = LETTER
MARGIN = 0.6 * inch
CONTENT_W = PAGE_W - 2 * MARGIN
CONTENT_H = PAGE_H - 2 * MARGIN


# ---------------------------------------------------------------------------
# Font selection.
# ---------------------------------------------------------------------------

# We stick to the four standard PDF Type-1 fonts (always available, no native
# deps). If a bundled mono/pixel font is ever shipped, register it here.
HEAD_FONT = "Helvetica-Bold"
BODY_FONT = "Helvetica"
ITALIC_FONT = "Helvetica-Oblique"
MONO_FONT = "Courier"
MONO_BOLD = "Courier-Bold"


# ---------------------------------------------------------------------------
# Payload.
# ---------------------------------------------------------------------------


@dataclass
class MemoPdfPayload:
    """Structured payload consumed by :func:`render_memo_pdf`."""

    ticker: str
    company_name: str
    memo_text: str
    recommendation: str
    sector: str | None = None
    industry: str | None = None
    generated_at: str | None = None

    target_low: float | None = None
    target_mid: float | None = None
    target_high: float | None = None
    current_price: float | None = None
    market_cap: float | None = None

    # Reclassification block.
    old_noun: str | None = None
    new_verb: str | None = None
    hidden_bom_role: str | None = None
    functional_layer: str | None = None
    proof_stage: int | None = None
    proof_stage_label: str | None = None
    reclassification_gap: float | None = None

    # Torque block.
    torque_score: float | None = None
    torque_stage: str | None = None
    torque_components: list[dict] | None = None

    memo_sections: dict[str, str] | None = None

    #: Product wordmark shown on the cover, the full-memo page and every footer.
    #: Defaults to the original Vision Memo branding so existing callers are
    #: unchanged; Prism passes "PRISM MEMO".
    document_title: str = "VISION MEMO"
    #: Label for the ``target_low`` cell. Prism puts a reassess/stop level there,
    #: which is not a target.
    target_low_label: str = "TARGET LOW"

    charts: list[dict] | None = None
    scenarios: list[dict] | None = None
    citations: list[dict] | None = None

    catalysts: list[str] | None = None
    kill_criteria: list[str] | None = None
    diligence_gaps: list[str] | None = field(default=None)


# ---------------------------------------------------------------------------
# Paragraph styles.
# ---------------------------------------------------------------------------


def _style(
    name: str,
    *,
    font: str = BODY_FONT,
    size: float = 10,
    leading: float | None = None,
    color: colors.Color = BODY,
    space_before: float = 0,
    space_after: float = 4,
    align: int = 0,
    upper: bool = False,
    tracking: float = 0,
    left_indent: float = 0,
) -> ParagraphStyle:
    return ParagraphStyle(
        name=name,
        fontName=font,
        fontSize=size,
        leading=leading if leading is not None else size * 1.35,
        textColor=color,
        spaceBefore=space_before,
        spaceAfter=space_after,
        alignment=align,
        leftIndent=left_indent,
        # ReportLab calls this `wordSpace`/`charSpace`; charSpace gives the
        # "tracking" feel for uppercase headings.
        spaceShrinkage=0.05,
        backColor=None,
        bulletFontName=BODY_FONT,
        bulletFontSize=size,
    )


STYLES: dict[str, ParagraphStyle] = {
    "body": _style("body", size=10, color=BODY, space_after=6),
    "muted": _style("muted", size=9, color=MUTED, space_after=4),
    "small": _style("small", size=8, color=MUTED, space_after=2),
    "strong": _style("strong", font=HEAD_FONT, size=10, color=STRONG, space_after=4),
    "h1": _style("h1", font=HEAD_FONT, size=20, color=AMBER, space_before=12, space_after=10),
    "h2": _style("h2", font=HEAD_FONT, size=16, color=AMBER, space_before=10, space_after=8),
    "h3": _style("h3", font=HEAD_FONT, size=13, color=AMBER, space_before=8, space_after=6),
    "section": _style(
        "section",
        font=HEAD_FONT,
        size=14,
        color=CYAN,
        space_before=10,
        space_after=8,
    ),
    "cover_ticker": _style(
        "cover_ticker",
        font=HEAD_FONT,
        size=72,
        leading=78,
        color=AMBER,
        space_after=2,
    ),
    "cover_company": _style(
        "cover_company", font=HEAD_FONT, size=18, color=CYAN, space_after=4
    ),
    "cover_meta": _style("cover_meta", size=11, color=MUTED, space_after=2),
    "cover_subtitle": _style(
        "cover_subtitle", font=HEAD_FONT, size=14, color=VIOLET, space_after=8
    ),
    "mono": _style("mono", font=MONO_FONT, size=9, color=BODY, space_after=2),
    "mono_bold": _style(
        "mono_bold", font=MONO_BOLD, size=10, color=STRONG, space_after=2
    ),
    "bullet_green": _style(
        "bullet_green", size=10, color=BODY, space_after=3, left_indent=14
    ),
    "bullet_red": _style(
        "bullet_red", size=10, color=BODY, space_after=3, left_indent=14
    ),
    "bullet_cyan": _style(
        "bullet_cyan", size=10, color=BODY, space_after=3, left_indent=14
    ),
    "caption": _style("caption", font=ITALIC_FONT, size=9, color=MUTED, space_after=8),
    "footer": _style("footer", size=8, color=MUTED, align=1),
}


# Derived list/citation styles. These have constant parameters, so we build
# them once at import and reuse them across every render instead of allocating
# a fresh ParagraphStyle per list item / citation row (which the hot markdown
# and section builders previously did on every call).
_LI_STYLES: dict[str, ParagraphStyle] = {
    "ul": ParagraphStyle(
        "li_ul",
        parent=STYLES["body"],
        leftIndent=14,
        firstLineIndent=-10,
        spaceAfter=2,
    ),
    "ol": ParagraphStyle(
        "li_ol",
        parent=STYLES["bullet_cyan"],
        leftIndent=14,
        firstLineIndent=-10,
        spaceAfter=2,
    ),
}

_LI_CAT_STYLE = ParagraphStyle(
    "li_cat",
    parent=STYLES["body"],
    leftIndent=12,
    firstLineIndent=-10,
    spaceAfter=4,
)

_CITE_STYLE = ParagraphStyle(
    "cite",
    parent=STYLES["body"],
    leftIndent=16,
    firstLineIndent=-16,
    spaceAfter=4,
)


# ---------------------------------------------------------------------------
# Markdown → flowable converter.
# ---------------------------------------------------------------------------


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+)$")
_NUM_RE = re.compile(r"^\s*\d+\.\s+(.+)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _md_inline(text: str) -> str:
    """Convert inline markdown into ReportLab paragraph markup."""

    safe = _xml_escape(text)
    safe = _LINK_RE.sub(
        lambda m: f'<link href="{m.group(2)}" color="#57d9ff">{m.group(1)}</link>',
        safe,
    )
    safe = _BOLD_RE.sub(r"<b>\1</b>", safe)
    safe = _ITALIC_RE.sub(r"<i>\1</i>", safe)
    safe = _CODE_RE.sub(
        lambda m: f'<font name="{MONO_FONT}" color="#79ff9c">{m.group(1)}</font>',
        safe,
    )
    return safe


def _split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _markdown_to_flowables(text: str) -> list[Flowable]:
    """Lightweight markdown → flowable converter.

    Handles headings (#, ##, ###), bold/italic/code/links inline, bullet and
    numbered lists, simple pipe tables, and paragraphs separated by blank
    lines. Robust to slightly malformed input.
    """

    if not text:
        return []

    lines = text.replace("\r\n", "\n").split("\n")
    out: list[Flowable] = []
    i = 0
    para_buf: list[str] = []
    list_buf: list[tuple[str, str]] = []  # (kind, content)

    def flush_para() -> None:
        if not para_buf:
            return
        joined = " ".join(s.strip() for s in para_buf if s.strip())
        para_buf.clear()
        if not joined:
            return
        try:
            out.append(Paragraph(_md_inline(joined), STYLES["body"]))
        except Exception:
            out.append(Paragraph(_xml_escape(joined), STYLES["body"]))

    def flush_list() -> None:
        if not list_buf:
            return
        for kind, content in list_buf:
            bullet = "&bull;" if kind == "ul" else "&#9656;"
            try:
                out.append(
                    Paragraph(
                        f'<font color="#57d9ff">{bullet}</font>&nbsp;&nbsp;'
                        + _md_inline(content),
                        _LI_STYLES.get(kind, _LI_STYLES["ul"]),
                    )
                )
            except Exception:
                out.append(Paragraph(_xml_escape(content), STYLES["body"]))
        list_buf.clear()

    while i < len(lines):
        line = lines[i]

        # Tables: header line followed by separator.
        if (
            "|" in line
            and i + 1 < len(lines)
            and _TABLE_SEP_RE.match(lines[i + 1] or "")
        ):
            flush_para()
            flush_list()
            header = _split_table_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_table_row(lines[i]))
                i += 1
            out.append(_simple_md_table(header, rows))
            continue

        stripped = line.strip()
        if not stripped:
            flush_para()
            flush_list()
            out.append(Spacer(1, 4))
            i += 1
            continue

        m = _HEADING_RE.match(stripped)
        if m:
            flush_para()
            flush_list()
            level = len(m.group(1))
            text_part = m.group(2)
            style_key = {1: "h1", 2: "h2", 3: "h3"}.get(level, "h3")
            try:
                out.append(Paragraph(_md_inline(text_part), STYLES[style_key]))
            except Exception:
                out.append(Paragraph(_xml_escape(text_part), STYLES[style_key]))
            i += 1
            continue

        m = _BULLET_RE.match(line)
        if m:
            flush_para()
            list_buf.append(("ul", m.group(1)))
            i += 1
            continue

        m = _NUM_RE.match(line)
        if m:
            flush_para()
            list_buf.append(("ol", m.group(1)))
            i += 1
            continue

        # Plain paragraph line.
        flush_list()
        para_buf.append(stripped)
        i += 1

    flush_para()
    flush_list()
    return out


def _simple_md_table(header: list[str], rows: list[list[str]]) -> Flowable:
    """Render a simple markdown table as a styled Table."""

    if not header:
        return Spacer(1, 0)

    def cell(text: str, style_key: str = "body") -> Paragraph:
        try:
            return Paragraph(_md_inline(text), STYLES[style_key])
        except Exception:
            return Paragraph(_xml_escape(text), STYLES[style_key])

    data: list[list[Any]] = [[cell(h, "strong") for h in header]]
    for row in rows:
        # Pad/truncate to header width.
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        else:
            row = row[: len(header)]
        data.append([cell(c) for c in row])

    col_w = CONTENT_W / max(1, len(header))
    tbl = Table(data, colWidths=[col_w] * len(header))
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), PANEL_BG),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, AMBER),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PANEL_BG, PANEL_BG_SOFT]),
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return tbl


# ---------------------------------------------------------------------------
# Custom flowables.
# ---------------------------------------------------------------------------


class HRule(Flowable):
    """A thin colored horizontal rule."""

    def __init__(
        self,
        width: float | None = None,
        color: colors.Color = RULE,
        thickness: float = 0.5,
    ) -> None:
        super().__init__()
        self._w = width
        self._color = color
        self._thickness = thickness

    def wrap(self, avail_w: float, avail_h: float) -> tuple[float, float]:
        self._w = self._w or avail_w
        return self._w, self._thickness + 2

    def draw(self) -> None:
        self.canv.setStrokeColor(self._color)
        self.canv.setLineWidth(self._thickness)
        self.canv.line(0, 1, self._w or 0, 1)


class Chip(Flowable):
    """A colored pill / chip used for ratings and labels."""

    def __init__(
        self,
        text: str,
        *,
        fill: colors.Color = AMBER,
        fg: colors.Color = PANEL_BG,
        font: str = HEAD_FONT,
        size: float = 14,
        pad_x: float = 14,
        pad_y: float = 6,
    ) -> None:
        super().__init__()
        self._text = text or ""
        self._fill = fill
        self._fg = fg
        self._font = font
        self._size = size
        self._pad_x = pad_x
        self._pad_y = pad_y

    def wrap(self, avail_w: float, avail_h: float) -> tuple[float, float]:
        w = pdfmetrics.stringWidth(self._text, self._font, self._size) + 2 * self._pad_x
        h = self._size + 2 * self._pad_y
        return w, h

    def draw(self) -> None:
        w, h = self.wrap(0, 0)
        c = self.canv
        c.setFillColor(self._fill)
        c.setStrokeColor(self._fill)
        c.roundRect(0, 0, w, h, h / 2, fill=1, stroke=0)
        c.setFillColor(self._fg)
        c.setFont(self._font, self._size)
        c.drawCentredString(w / 2, self._pad_y + self._size * 0.18, self._text)


class HBar(Flowable):
    """A horizontal score bar for torque components."""

    def __init__(
        self,
        score: float,
        *,
        width: float = 2.5 * inch,
        height: float = 8,
        color: colors.Color = AMBER,
        bg: colors.Color = PANEL_BG_SOFT,
    ) -> None:
        super().__init__()
        try:
            self._score = max(0.0, min(100.0, float(score)))
        except (TypeError, ValueError):
            self._score = 0.0
        self._w = width
        self._h = height
        self._color = color
        self._bg = bg

    def wrap(self, avail_w: float, avail_h: float) -> tuple[float, float]:
        return self._w, self._h

    def draw(self) -> None:
        c = self.canv
        c.setFillColor(self._bg)
        c.setStrokeColor(RULE)
        c.rect(0, 0, self._w, self._h, fill=1, stroke=0)
        filled = self._w * (self._score / 100.0)
        c.setFillColor(self._color)
        c.rect(0, 0, filled, self._h, fill=1, stroke=0)


# ---------------------------------------------------------------------------
# Page decoration (background, scan lines, footer).
# ---------------------------------------------------------------------------


class _MemoCanvas(Canvas):
    """Canvas subclass that paints the dark page background and scan lines."""

    def __init__(
        self,
        *args: Any,
        ticker: str = "",
        document_title: str = "VISION MEMO",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._ticker = ticker
        self._document_title = document_title
        self._saved_pages: list[dict] = []

    def showPage(self) -> None:  # type: ignore[override]
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:  # type: ignore[override]
        page_count = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            self._paint_footer(page_count)
            super().showPage()
        super().save()

    def _paint_footer(self, page_count: int) -> None:
        # Skip footer on cover (page 1) — keep it clean.
        if self._pageNumber == 1:
            return
        self.setFillColor(MUTED)
        self.setFont(BODY_FONT, 8)
        footer_y = MARGIN / 2
        title = self._document_title or "VISION MEMO"
        left_text = self._ticker.upper() if self._ticker else title.upper()
        self.drawString(MARGIN, footer_y, f"{left_text} — {title.title()}")
        self.drawRightString(
            PAGE_W - MARGIN,
            footer_y,
            f"Page {self._pageNumber} of {page_count}",
        )


def _paint_background(canvas: Canvas, _doc: BaseDocTemplate) -> None:
    """Page background, scan lines, top frame."""

    canvas.saveState()
    # Outer page fill.
    canvas.setFillColor(PAGE_BG)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # Inner content panel — slightly darker.
    canvas.setFillColor(PANEL_BG)
    canvas.roundRect(
        MARGIN - 4,
        MARGIN - 6,
        CONTENT_W + 8,
        CONTENT_H + 12,
        4,
        fill=1,
        stroke=0,
    )

    # Subtle scan lines (every 4pt, very low opacity).
    canvas.setStrokeColor(SCANLINE)
    canvas.setLineWidth(0.4)
    y = MARGIN
    while y < PAGE_H - MARGIN:
        canvas.line(MARGIN, y, PAGE_W - MARGIN, y)
        y += 4
    canvas.restoreState()


def _paint_cover_background(canvas: Canvas, doc: BaseDocTemplate) -> None:
    """Same as the regular background plus a giant ticker watermark."""

    _paint_background(canvas, doc)
    ticker = getattr(doc, "_ticker_watermark", "")
    if not ticker:
        return
    canvas.saveState()
    canvas.setFillColor(colors.Color(1, 0.79, 0.29, alpha=0.04))
    canvas.setFont(HEAD_FONT, 240)
    canvas.drawCentredString(PAGE_W / 2, PAGE_H / 2 - 80, ticker[:5])
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _fmt_money(value: float | None, *, suffix: str = "") -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1_000_000_000_000:
        return f"${v / 1e12:.2f}T{suffix}"
    if abs(v) >= 1_000_000_000:
        return f"${v / 1e9:.2f}B{suffix}"
    if abs(v) >= 1_000_000:
        return f"${v / 1e6:.2f}M{suffix}"
    if abs(v) >= 1_000:
        return f"${v / 1e3:.2f}K{suffix}"
    return f"${v:,.2f}{suffix}"


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _recommendation_color(rec: str | None) -> colors.Color:
    if not rec:
        return MUTED
    norm = rec.strip().lower()
    if "strong buy" in norm or norm in {"buy", "outperform", "overweight"}:
        return GREEN
    if "sell" in norm or "underperform" in norm or "underweight" in norm:
        return RED
    if "hold" in norm or "neutral" in norm or "market" in norm:
        return AMBER
    return AMBER


def _decode_image(data: str | bytes | None) -> BytesIO | None:
    """Decode a base64 PNG/JPEG payload into a BytesIO. Returns None on error."""

    if not data:
        return None
    try:
        if isinstance(data, bytes):
            raw = data
        else:
            txt = str(data)
            if "," in txt and txt.lstrip().startswith("data:"):
                txt = txt.split(",", 1)[1]
            raw = base64.b64decode(txt, validate=False)
        if not raw:
            return None
        bio = BytesIO(raw)
        # Probe with ImageReader so we fail here, not deep in platypus.
        ImageReader(bio).getSize()
        bio.seek(0)
        return bio
    except (binascii.Error, ValueError, OSError, Exception):
        return None


def _safe_paragraph(text: str, style_key: str = "body", *, raw: bool = False) -> Paragraph:
    """A paragraph, with markdown inline conversion unless ``raw`` is set.

    ``_md_inline`` XML-escapes its whole input, so a caller that deliberately
    built ReportLab markup (``<font color="...">``) used to have that markup drawn
    as literal visible text. ``raw=True`` skips the conversion for strings the
    caller already escaped itself.
    """
    style = STYLES.get(style_key, STYLES["body"])
    if raw:
        try:
            return Paragraph(text, style)
        except Exception:
            return Paragraph(_xml_escape(text or ""), style)
    try:
        return Paragraph(_md_inline(text), style)
    except Exception:
        return Paragraph(_xml_escape(text or ""), style)


# ---------------------------------------------------------------------------
# Section builders.
# ---------------------------------------------------------------------------


def _build_cover(payload: MemoPdfPayload) -> list[Flowable]:
    story: list[Flowable] = []
    story.append(Spacer(1, 0.35 * inch))
    story.append(_safe_paragraph(payload.document_title, "cover_subtitle"))
    story.append(_safe_paragraph(payload.ticker.upper(), "cover_ticker"))
    story.append(_safe_paragraph(payload.company_name, "cover_company"))

    meta_bits: list[str] = []
    if payload.sector:
        meta_bits.append(payload.sector)
    if payload.industry:
        meta_bits.append(payload.industry)
    if meta_bits:
        story.append(_safe_paragraph(" · ".join(meta_bits), "cover_meta"))

    story.append(Spacer(1, 0.25 * inch))
    story.append(HRule(width=CONTENT_W, color=AMBER, thickness=1))
    story.append(Spacer(1, 0.18 * inch))

    rec_color = _recommendation_color(payload.recommendation)
    chip_label = (payload.recommendation or "REVIEW").upper()
    chip = Chip(chip_label, fill=rec_color, fg=PANEL_BG, size=16, pad_x=18, pad_y=8)

    target_cells: list[list[Any]] = [
        [
            _safe_paragraph(payload.target_low_label, "small"),
            _safe_paragraph("TARGET MID", "small"),
            _safe_paragraph("TARGET HIGH", "small"),
            _safe_paragraph("CURRENT", "small"),
        ],
        [
            Paragraph(
                f'<font name="{MONO_BOLD}" size="16" color="#ff695d">'
                f"{_fmt_price(payload.target_low)}</font>",
                STYLES["body"],
            ),
            Paragraph(
                f'<font name="{MONO_BOLD}" size="20" color="#ffc94a">'
                f"<b>{_fmt_price(payload.target_mid)}</b></font>",
                STYLES["body"],
            ),
            Paragraph(
                f'<font name="{MONO_BOLD}" size="16" color="#79ff9c">'
                f"{_fmt_price(payload.target_high)}</font>",
                STYLES["body"],
            ),
            Paragraph(
                f'<font name="{MONO_BOLD}" size="16" color="#d4dde5">'
                f"{_fmt_price(payload.current_price)}</font>",
                STYLES["body"],
            ),
        ],
    ]
    target_table = Table(
        target_cells,
        colWidths=[CONTENT_W / 4.0] * 4,
        rowHeights=[14, 30],
    )
    target_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, -1), PANEL_BG_SOFT),
                ("BOX", (0, 0), (-1, -1), 0.4, RULE),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    chip_row = Table(
        [[chip, target_table]],
        colWidths=[2.0 * inch, CONTENT_W - 2.0 * inch],
    )
    chip_row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(chip_row)

    story.append(Spacer(1, 0.25 * inch))

    # Stage label.
    stage_text_parts: list[str] = []
    if payload.torque_stage:
        stage_text_parts.append(payload.torque_stage)
    if payload.proof_stage is not None and payload.proof_stage_label:
        stage_text_parts.append(
            f"Stage {payload.proof_stage}: {payload.proof_stage_label}"
        )
    elif payload.proof_stage_label:
        stage_text_parts.append(payload.proof_stage_label)
    if stage_text_parts:
        story.append(
            _safe_paragraph(
                f'<font color="#b28cff"><b>'
                + " · ".join(_xml_escape(s) for s in stage_text_parts)
                + "</b></font>",
                "section",
                raw=True,
            )
        )

    # Key metrics row.
    metric_pairs: list[tuple[str, str]] = []
    if payload.market_cap is not None:
        metric_pairs.append(("MARKET CAP", _fmt_money(payload.market_cap)))
    if payload.torque_score is not None:
        try:
            metric_pairs.append(("TORQUE", f"{float(payload.torque_score):.1f}/100"))
        except (TypeError, ValueError):
            pass
    if payload.reclassification_gap is not None:
        try:
            metric_pairs.append(
                ("RECLASS GAP", f"{float(payload.reclassification_gap) * 100:.0f}%")
            )
        except (TypeError, ValueError):
            pass
    if metric_pairs:
        story.append(Spacer(1, 0.15 * inch))
        story.append(_metric_row(metric_pairs))

    story.append(Spacer(1, 0.4 * inch))
    if payload.generated_at:
        story.append(
            _safe_paragraph(
                f"Generated {_xml_escape(payload.generated_at)}",
                "muted",
            )
        )
    return story


def _metric_row(pairs: list[tuple[str, str]]) -> Flowable:
    cells_top = [_safe_paragraph(label, "small") for label, _ in pairs]
    cells_bot = [
        Paragraph(
            f'<font name="{MONO_BOLD}" size="13" color="#ffffff">'
            f"{_xml_escape(value)}</font>",
            STYLES["body"],
        )
        for _, value in pairs
    ]
    col_w = CONTENT_W / max(1, len(pairs))
    tbl = Table([cells_top, cells_bot], colWidths=[col_w] * len(pairs))
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PANEL_BG_SOFT),
                ("BOX", (0, 0), (-1, -1), 0.4, RULE),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ]
        )
    )
    return tbl


def _executive_read(payload: MemoPdfPayload) -> list[Flowable]:
    story: list[Flowable] = [_safe_paragraph("EXECUTIVE READ", "h1")]

    section_text: str | None = None
    if payload.memo_sections:
        for key in (
            "Executive Read",
            "Executive Summary",
            "Summary",
            "executive_read",
        ):
            if key in payload.memo_sections and payload.memo_sections[key]:
                section_text = payload.memo_sections[key]
                break

    if not section_text and payload.memo_text:
        words = payload.memo_text.split()
        section_text = " ".join(words[:600])

    if section_text:
        story.extend(_markdown_to_flowables(section_text))
    else:
        story.append(_safe_paragraph("No executive read available.", "muted"))

    if any(
        v is not None
        for v in (
            payload.old_noun,
            payload.new_verb,
            payload.hidden_bom_role,
            payload.functional_layer,
        )
    ):
        story.append(Spacer(1, 0.2 * inch))
        story.append(_reclassification_callout(payload))

    return story


def _reclassification_callout(payload: MemoPdfPayload) -> Flowable:
    old = payload.old_noun or "—"
    new = payload.new_verb or "—"

    rows: list[list[Any]] = []
    rows.append(
        [
            _safe_paragraph("OLD NOUN → NEW VERB", "small"),
        ]
    )
    rows.append(
        [
            Paragraph(
                f'<font color="#ff695d"><strike>{_xml_escape(old)}</strike></font>'
                f'&nbsp;&nbsp;<font color="#57d9ff">→</font>&nbsp;&nbsp;'
                f'<font color="#79ff9c"><b>{_xml_escape(new)}</b></font>',
                STYLES["body"],
            )
        ]
    )
    if payload.hidden_bom_role:
        rows.append(
            [
                Paragraph(
                    f'<font color="#5a6470">Hidden BOM role:</font> '
                    f'<font color="#ffffff">{_xml_escape(payload.hidden_bom_role)}</font>',
                    STYLES["body"],
                )
            ]
        )
    if payload.functional_layer:
        rows.append(
            [
                Paragraph(
                    f'<font color="#5a6470">Functional layer:</font> '
                    f'<font color="#b28cff"><b>{_xml_escape(payload.functional_layer)}</b></font>',
                    STYLES["body"],
                )
            ]
        )

    tbl = Table(rows, colWidths=[CONTENT_W])
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), PANEL_BG_SOFT),
                ("BOX", (0, 0), (-1, -1), 0.6, CYAN),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return tbl


def _torque_section(payload: MemoPdfPayload) -> list[Flowable]:
    story: list[Flowable] = [_safe_paragraph("TORQUE INDICATOR", "h1")]

    score_str = "—"
    if payload.torque_score is not None:
        try:
            score_str = f"{float(payload.torque_score):.1f}"
        except (TypeError, ValueError):
            score_str = str(payload.torque_score)

    big = Paragraph(
        f'<font name="{MONO_BOLD}" size="60" color="#ffc94a">'
        f"{_xml_escape(score_str)}</font>"
        f'<font name="{HEAD_FONT}" size="20" color="#5a6470">/100</font>',
        STYLES["body"],
    )
    stage = _safe_paragraph(
        f'<font color="#b28cff"><b>{_xml_escape(payload.torque_stage or "")}</b></font>',
        "section",
        raw=True,
    )

    score_table = Table([[big], [stage]], colWidths=[CONTENT_W])
    score_table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    story.append(score_table)
    story.append(Spacer(1, 0.15 * inch))

    if payload.torque_components:
        story.append(_safe_paragraph("COMPONENTS", "section"))
        rows: list[list[Any]] = []
        for comp in payload.torque_components:
            if not isinstance(comp, dict):
                continue
            name = str(comp.get("name") or "—")
            score = comp.get("score")
            weight = comp.get("weight")
            detail = str(comp.get("detail") or "")
            try:
                score_f = float(score) if score is not None else 0.0
            except (TypeError, ValueError):
                score_f = 0.0
            try:
                weight_str = f"{float(weight) * 100:.0f}%" if weight is not None else ""
            except (TypeError, ValueError):
                weight_str = str(weight) if weight is not None else ""

            bar_color = (
                GREEN if score_f >= 66 else AMBER if score_f >= 33 else RED
            )
            rows.append(
                [
                    Paragraph(
                        f'<font color="#ffffff"><b>{_xml_escape(name)}</b></font>',
                        STYLES["body"],
                    ),
                    HBar(score_f, color=bar_color, width=2.0 * inch),
                    Paragraph(
                        f'<font name="{MONO_BOLD}" color="#ffc94a">{score_f:.0f}</font>'
                        f'<font color="#5a6470"> · {_xml_escape(weight_str)}</font>',
                        STYLES["body"],
                    ),
                    _safe_paragraph(detail, "muted"),
                ]
            )
        if rows:
            tbl = Table(
                rows,
                colWidths=[
                    1.4 * inch,
                    2.1 * inch,
                    0.9 * inch,
                    CONTENT_W - 4.4 * inch,
                ],
            )
            tbl.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("BACKGROUND", (0, 0), (-1, -1), PANEL_BG_SOFT),
                        ("BOX", (0, 0), (-1, -1), 0.4, RULE),
                        ("INNERGRID", (0, 0), (-1, -1), 0.2, RULE),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.append(tbl)

    # Inline torque chart if present.
    if payload.charts:
        for chart in payload.charts:
            if not isinstance(chart, dict):
                continue
            title = str(chart.get("title") or "").lower()
            if "torque" in title:
                img = _chart_image(chart, max_h=2.6 * inch)
                if img is not None:
                    story.append(Spacer(1, 0.15 * inch))
                    story.extend(img)
                break

    return story


def _memo_body(payload: MemoPdfPayload) -> list[Flowable]:
    story: list[Flowable] = [_safe_paragraph(payload.document_title, "h1")]
    if not payload.memo_text:
        story.append(_safe_paragraph("No memo body available.", "muted"))
        return story
    story.extend(_markdown_to_flowables(payload.memo_text))
    return story


def _scenarios_section(payload: MemoPdfPayload) -> list[Flowable]:
    if not payload.scenarios:
        return []
    story: list[Flowable] = [_safe_paragraph("SCENARIO FRAMEWORK", "h1")]

    headers = [
        "Scenario",
        "Revenue",
        "Gross Margin",
        "EPS",
        "Multiple",
        "Implied Price",
        "Notes",
    ]
    data: list[list[Any]] = [
        [_safe_paragraph(h, "strong") for h in headers],
    ]
    style_cmds: list[Any] = [
        ("BACKGROUND", (0, 0), (-1, 0), PANEL_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, AMBER),
        ("BOX", (0, 0), (-1, -1), 0.4, RULE),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for i, scen in enumerate(payload.scenarios):
        if not isinstance(scen, dict):
            continue
        name = str(scen.get("name") or "—")
        norm = name.lower()
        if "bull" in norm:
            color = GREEN
        elif "bear" in norm:
            color = RED
        else:
            color = BODY
        row = [
            Paragraph(
                f'<font color="{color.hexval()}"><b>{_xml_escape(name)}</b></font>',
                STYLES["body"],
            ),
            _safe_paragraph(str(scen.get("rev_growth") or "—")),
            _safe_paragraph(str(scen.get("gm") or "—")),
            _safe_paragraph(str(scen.get("eps") or "—")),
            _safe_paragraph(str(scen.get("multiple") or "—")),
            Paragraph(
                f'<font name="{MONO_BOLD}" color="{color.hexval()}">'
                f"{_xml_escape(str(scen.get('price') or '—'))}</font>",
                STYLES["body"],
            ),
            _safe_paragraph(str(scen.get("notes") or ""), "muted"),
        ]
        data.append(row)
        row_idx = len(data) - 1
        style_cmds.append(
            ("BACKGROUND", (0, row_idx), (-1, row_idx),
             PANEL_BG if i % 2 == 0 else PANEL_BG_SOFT)
        )

    col_w = [0.9 * inch, 0.9 * inch, 1.0 * inch, 0.7 * inch, 0.8 * inch, 1.1 * inch]
    col_w.append(CONTENT_W - sum(col_w))
    tbl = Table(data, colWidths=col_w)
    tbl.setStyle(TableStyle(style_cmds))
    story.append(tbl)

    story.append(Spacer(1, 0.18 * inch))
    story.append(
        _safe_paragraph(
            "Scenario prices are derived from forward EPS × applied multiple, "
            "tested against base assumptions. Bear/Base/Bull bracket the "
            "distribution of outcomes implied by the reclassification thesis.",
            "muted",
        )
    )
    return story


def _catalysts_section(payload: MemoPdfPayload) -> list[Flowable]:
    if not (payload.catalysts or payload.kill_criteria or payload.diligence_gaps):
        return []
    story: list[Flowable] = [
        _safe_paragraph("CATALYSTS · KILL CRITERIA · DILIGENCE GAPS", "h1"),
    ]

    def column(title: str, items: list[str] | None, color: colors.Color) -> list[Flowable]:
        col: list[Flowable] = [
            Paragraph(
                f'<font color="{color.hexval()}"><b>{_xml_escape(title)}</b></font>',
                STYLES["section"],
            )
        ]
        if not items:
            col.append(_safe_paragraph("—", "muted"))
            return col
        for item in items:
            col.append(
                Paragraph(
                    f'<font color="{color.hexval()}">&bull;</font>&nbsp;&nbsp;'
                    + _md_inline(str(item)),
                    _LI_CAT_STYLE,
                )
            )
        return col

    col_w = CONTENT_W / 3.0
    cells = [
        [
            column("CATALYSTS", payload.catalysts, GREEN),
            column("KILL CRITERIA", payload.kill_criteria, RED),
            column("DILIGENCE GAPS", payload.diligence_gaps, CYAN),
        ]
    ]
    tbl = Table(cells, colWidths=[col_w] * 3)
    tbl.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), PANEL_BG_SOFT),
                ("BACKGROUND", (1, 0), (1, -1), PANEL_BG_SOFT),
                ("BACKGROUND", (2, 0), (2, -1), PANEL_BG_SOFT),
                ("BOX", (0, 0), (-1, -1), 0.4, RULE),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(tbl)
    return story


def _chart_image(chart: dict, *, max_h: float = 4.0 * inch) -> list[Flowable] | None:
    bio = _decode_image(chart.get("data") or chart.get("image_data"))
    if bio is None:
        return None
    try:
        reader = ImageReader(bio)
        iw, ih = reader.getSize()
        if iw <= 0 or ih <= 0:
            return None
        ratio = ih / iw
        target_w = CONTENT_W
        target_h = target_w * ratio
        if target_h > max_h:
            target_h = max_h
            target_w = target_h / ratio
        bio.seek(0)
        img = Image(bio, width=target_w, height=target_h)
    except Exception:
        return None

    pieces: list[Flowable] = []
    title = chart.get("title")
    if title:
        pieces.append(_safe_paragraph(str(title), "section"))
    pieces.append(img)
    caption = chart.get("caption")
    if caption:
        pieces.append(_safe_paragraph(str(caption), "caption"))
    return pieces


def _charts_section(payload: MemoPdfPayload) -> list[Flowable]:
    if not payload.charts:
        return []
    story: list[Flowable] = [_safe_paragraph("CHARTS", "h1")]
    rendered = 0
    for chart in payload.charts:
        if not isinstance(chart, dict):
            continue
        title = str(chart.get("title") or "").lower()
        # Skip the torque chart — already rendered inline.
        if "torque" in title:
            continue
        pieces = _chart_image(chart)
        if not pieces:
            continue
        story.append(KeepTogether(pieces))
        story.append(Spacer(1, 0.2 * inch))
        rendered += 1
    if rendered == 0:
        return []
    return story


def _citations_section(payload: MemoPdfPayload) -> list[Flowable]:
    if not payload.citations:
        return []
    story: list[Flowable] = [_safe_paragraph("CITATIONS", "h1")]
    for i, cite in enumerate(payload.citations, start=1):
        if not isinstance(cite, dict):
            continue
        label = str(cite.get("label") or cite.get("title") or f"Citation {i}")
        source = str(cite.get("source") or "")
        url = str(cite.get("url") or "")
        filed = str(cite.get("filed_date") or cite.get("date") or "")

        parts: list[str] = [f'<b>[{i}]</b> <font color="#ffffff">{_xml_escape(label)}</font>']
        if source:
            parts.append(f'<font color="#5a6470">{_xml_escape(source)}</font>')
        if filed:
            parts.append(f'<font color="#b28cff">{_xml_escape(filed)}</font>')
        if url:
            parts.append(
                f'<link href="{_xml_escape(url)}" color="#57d9ff">{_xml_escape(url)}</link>'
            )
        body = " — ".join(parts)
        story.append(
            Paragraph(
                body,
                _CITE_STYLE,
            )
        )
    return story


# ---------------------------------------------------------------------------
# Document assembly.
# ---------------------------------------------------------------------------


def _build_doc(buffer: BytesIO, payload: MemoPdfPayload) -> BaseDocTemplate:
    doc = BaseDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=f"{payload.ticker} {payload.document_title.title()}",
        author="Underlying Analyzer",
    )

    frame = Frame(
        MARGIN,
        MARGIN,
        CONTENT_W,
        CONTENT_H,
        leftPadding=12,
        rightPadding=12,
        topPadding=12,
        bottomPadding=12,
        showBoundary=0,
    )
    cover_template = PageTemplate(
        id="cover", frames=[frame], onPage=_paint_cover_background
    )
    body_template = PageTemplate(
        id="body", frames=[frame], onPage=_paint_background
    )
    doc.addPageTemplates([cover_template, body_template])

    setattr(doc, "_ticker_watermark", (payload.ticker or "").upper()[:6])
    return doc


def _build_story(payload: MemoPdfPayload) -> list[Flowable]:
    story: list[Flowable] = []

    # Cover.
    story.extend(_build_cover(payload))
    story.append(PageBreak())

    # Switch to body template for everything else.
    from reportlab.platypus import NextPageTemplate

    story.insert(len(story), NextPageTemplate("body"))

    # Executive read + reclassification.
    story.extend(_executive_read(payload))
    story.append(PageBreak())

    # Torque.
    if (
        payload.torque_score is not None
        or payload.torque_stage
        or payload.torque_components
    ):
        story.extend(_torque_section(payload))
        story.append(PageBreak())

    # Memo body.
    body = _memo_body(payload)
    if body:
        story.extend(body)
        story.append(PageBreak())

    # Scenarios.
    scen = _scenarios_section(payload)
    if scen:
        story.extend(scen)
        story.append(PageBreak())

    # Catalysts / kill / diligence.
    cats = _catalysts_section(payload)
    if cats:
        story.extend(cats)
        story.append(PageBreak())

    # Charts.
    charts = _charts_section(payload)
    if charts:
        story.extend(charts)
        story.append(PageBreak())

    # Citations.
    cits = _citations_section(payload)
    if cits:
        story.extend(cits)

    # Trim trailing PageBreak to avoid a blank last page.
    while story and isinstance(story[-1], PageBreak):
        story.pop()

    return story


def _error_pdf(ticker: str, message: str) -> bytes:
    """Minimal fallback PDF used when something catastrophic happens."""

    try:
        buf = BytesIO()
        c = Canvas(buf, pagesize=LETTER)
        c.setFillColor(PAGE_BG)
        c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
        c.setFillColor(AMBER)
        c.setFont(HEAD_FONT, 40)
        c.drawString(MARGIN, PAGE_H - MARGIN - 40, (ticker or "MEMO").upper())
        c.setFillColor(BODY)
        c.setFont(BODY_FONT, 11)
        c.drawString(
            MARGIN,
            PAGE_H - MARGIN - 70,
            "Vision Memo — PDF could not be rendered.",
        )
        c.setFillColor(MUTED)
        c.setFont(BODY_FONT, 9)
        # Wrap message in ~95 char chunks.
        msg = (message or "").strip()
        y = PAGE_H - MARGIN - 100
        for chunk_start in range(0, min(len(msg), 1500), 95):
            c.drawString(MARGIN, y, msg[chunk_start : chunk_start + 95])
            y -= 12
            if y < MARGIN:
                break
        c.showPage()
        c.save()
        return buf.getvalue()
    except Exception:  # pragma: no cover - last-ditch fallback
        # Hand-crafted minimal valid PDF.
        return (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n"
            b"0000000010 00000 n \n0000000053 00000 n \n0000000100 00000 n \n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n160\n%%EOF\n"
        )


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def render_memo_pdf(payload: MemoPdfPayload) -> bytes:
    """Render ``payload`` to PDF bytes. Never raises.

    Missing optional fields cause their sections to be skipped. If the render
    fails catastrophically, a minimal "error" PDF is returned instead.
    """

    # Coerce a wildly broken payload back to something usable. We deliberately
    # don't raise on missing required fields — instead we substitute safe
    # defaults so callers can ship _something_.
    try:
        if not isinstance(payload, MemoPdfPayload):
            return _error_pdf("UNKNOWN", "Invalid payload type.")
        ticker = (payload.ticker or "MEMO").strip() or "MEMO"
        company = (payload.company_name or "").strip()
        memo = payload.memo_text if isinstance(payload.memo_text, str) else ""
        rec = payload.recommendation if isinstance(payload.recommendation, str) else ""
        # Build a normalized copy with safe required fields.
        safe = MemoPdfPayload(
            ticker=ticker,
            company_name=company,
            memo_text=memo,
            recommendation=rec,
            sector=payload.sector,
            industry=payload.industry,
            generated_at=payload.generated_at,
            target_low=payload.target_low,
            target_mid=payload.target_mid,
            target_high=payload.target_high,
            current_price=payload.current_price,
            market_cap=payload.market_cap,
            old_noun=payload.old_noun,
            new_verb=payload.new_verb,
            hidden_bom_role=payload.hidden_bom_role,
            functional_layer=payload.functional_layer,
            proof_stage=payload.proof_stage,
            proof_stage_label=payload.proof_stage_label,
            reclassification_gap=payload.reclassification_gap,
            torque_score=payload.torque_score,
            torque_stage=payload.torque_stage,
            torque_components=payload.torque_components,
            memo_sections=payload.memo_sections,
            document_title=(payload.document_title or "VISION MEMO").strip() or "VISION MEMO",
            target_low_label=(payload.target_low_label or "TARGET LOW").strip() or "TARGET LOW",
            charts=payload.charts,
            scenarios=payload.scenarios,
            citations=payload.citations,
            catalysts=payload.catalysts,
            kill_criteria=payload.kill_criteria,
            diligence_gaps=payload.diligence_gaps,
        )
    except Exception as exc:
        logger.exception("memo_pdf payload normalization failed: %s", exc)
        return _error_pdf(
            getattr(payload, "ticker", "") or "MEMO",
            f"Payload normalization failed: {exc}",
        )

    try:
        buf = BytesIO()
        doc = _build_doc(buf, safe)
        story = _build_story(safe)
        doc.build(
            story,
            canvasmaker=lambda *a, **kw: _MemoCanvas(
                *a,
                ticker=safe.ticker.upper(),
                document_title=safe.document_title,
                **kw
            ),
        )
        return buf.getvalue()
    except Exception as exc:
        logger.exception("memo_pdf render failed for %s: %s", safe.ticker, exc)
        return _error_pdf(safe.ticker, str(exc))


__all__ = ["MemoPdfPayload", "render_memo_pdf"]
