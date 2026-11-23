"""Minimal .xlsx (Office Open XML) writer — no dependencies, pure logic (testable without Home Assistant).

Supports: several sheets, bold header row (frozen, with autofilter), column widths, numbers with a
number format per column, dates / date-times as real Excel dates, text, and SUM formulas in a totals row.
"""
from __future__ import annotations

import datetime as dt
import io
import zipfile
from dataclasses import dataclass, field
from typing import Any, Sequence
from xml.sax.saxutils import escape

# style ids (cellXfs order in styles.xml)
S_TEXT, S_HEAD, S_DATE, S_DATETIME, S_NUM3, S_NUM2, S_PCT, S_MONTH, S_TOTAL3, S_TOTAL2, S_TOTAL_TEXT = range(11)
FORMATS = {"text": S_TEXT, "date": S_DATE, "datetime": S_DATETIME, "kwh": S_NUM3, "money": S_NUM2,
           "pct": S_PCT, "month": S_MONTH}
TOTAL_STYLE = {"kwh": S_TOTAL3, "money": S_TOTAL2}
_EPOCH = dt.datetime(1899, 12, 30)


@dataclass
class Column:
    title: str
    fmt: str = "text"          # text | date | datetime | month | kwh | money | pct
    width: float = 14
    total: bool = False        # SUM in the totals row


@dataclass
class Sheet:
    name: str
    columns: list[Column]
    rows: list[Sequence[Any]] = field(default_factory=list)
    totals_label: str | None = None  # adds a totals row (SUM of columns with total=True)


def col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def excel_serial(value: dt.date | dt.datetime) -> float:
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.replace(tzinfo=None)   # caller passes local time
        return (value - _EPOCH).total_seconds() / 86400
    return float((value - _EPOCH.date()).days)


def _cell(ref: str, value: Any, style: int) -> str:
    if value is None or value == "":
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(value, (dt.date, dt.datetime)):
        return f'<c r="{ref}" s="{style}"><v>{excel_serial(value):.15g}</v></c>'
    if isinstance(value, bool):
        return f'<c r="{ref}" s="{style}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{value!r}</v></c>'
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def _sheet_xml(sheet: Sheet) -> str:
    ncol = len(sheet.columns)
    last_col = col_letter(ncol - 1)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
           'state="frozen"/></sheetView></sheetViews><cols>']
    for i, c in enumerate(sheet.columns, 1):
        out.append(f'<col min="{i}" max="{i}" width="{c.width}" customWidth="1"/>')
    out.append('</cols><sheetData>')
    out.append('<row r="1">' + "".join(_cell(f"{col_letter(i)}1", c.title, S_HEAD)
                                       for i, c in enumerate(sheet.columns)) + "</row>")
    for r, row in enumerate(sheet.rows, 2):
        cells = [_cell(f"{col_letter(i)}{r}", v, FORMATS.get(sheet.columns[i].fmt, S_TEXT))
                 for i, v in enumerate(row[:ncol])]
        out.append(f'<row r="{r}">' + "".join(cells) + "</row>")
    last_data = len(sheet.rows) + 1
    if sheet.totals_label is not None and sheet.rows:
        r = last_data + 1
        cells = [_cell(f"A{r}", sheet.totals_label, S_TOTAL_TEXT)]
        for i, c in enumerate(sheet.columns[1:], 1):
            ref = f"{col_letter(i)}{r}"
            if c.total:
                rng = f"{col_letter(i)}2:{col_letter(i)}{last_data}"
                cells.append(f'<c r="{ref}" s="{TOTAL_STYLE.get(c.fmt, S_TOTAL2)}"><f>SUM({rng})</f></c>')
            else:
                cells.append(f'<c r="{ref}" s="{S_TOTAL_TEXT}"/>')
        out.append(f'<row r="{r}">' + "".join(cells) + "</row>")
    out.append("</sheetData>")
    if sheet.rows:
        out.append(f'<autoFilter ref="A1:{last_col}{last_data}"/>')
    out.append("</worksheet>")
    return "".join(out)


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="5"><numFmt numFmtId="164" formatCode="dd.mm.yyyy"/>'
    '<numFmt numFmtId="165" formatCode="dd.mm.yyyy hh:mm"/><numFmt numFmtId="166" formatCode="0.000"/>'
    '<numFmt numFmtId="167" formatCode="0.0&quot; %&quot;"/><numFmt numFmtId="168" formatCode="mmmm yyyy"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFE8EEF4"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="2"><border/><border><top style="thin"/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="11">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'                                  # text
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'       # header
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'           # date
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'           # datetime
    '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'           # kWh
    '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'             # money #,##0.00
    '<xf numFmtId="167" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'           # pct
    '<xf numFmtId="168" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'           # month
    '<xf numFmtId="166" fontId="1" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1"/>'
    '<xf numFmtId="4" fontId="1" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1"/>'
    '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
)


def build_xlsx(sheets: list[Sheet]) -> bytes:
    """Return the bytes of an .xlsx workbook with the given sheets."""
    n = len(sheets)
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" '
          'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" '
          'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    for i in range(1, n + 1))
          + "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + "".join(f'<sheet name="{escape(s.name[:31])}" sheetId="{i}" r:id="rId{i}"/>'
                    for i, s in enumerate(sheets, 1))
          + "</sheets></workbook>")
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                         f'relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, n + 1))
               + f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                 'relationships/styles" Target="styles.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", _STYLES)
        for i, s in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(s))
    return buf.getvalue()
