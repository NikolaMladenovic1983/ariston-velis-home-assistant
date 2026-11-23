"""Tests for the dependency-free xlsx writer. Run: python -I tests/test_xlsx_writer.py"""
import datetime as dt
import importlib.util
import io
import pathlib
import sys
import zipfile
import xml.dom.minidom

_P = pathlib.Path(__file__).resolve().parent.parent / "ha-config/custom_components/ariston_schedule/xlsx_writer.py"
_spec = importlib.util.spec_from_file_location("xlsx_writer", _P)
xw = importlib.util.module_from_spec(_spec)
sys.modules["xlsx_writer"] = xw
_spec.loader.exec_module(xw)


def sample() -> bytes:
    cols = [xw.Column("Datum", "date"), xw.Column("kWh jeftina", "kwh", total=True),
            xw.Column("RSD", "money", total=True), xw.Column("Udeo", "pct")]
    rows = [[dt.date(2026, 11, 22), 4.0, 14.0, 100.0], [dt.date(2026, 11, 23), 2.35, 8.23, None]]
    return xw.build_xlsx([xw.Sheet("Po danima", cols, rows, totals_label="Ukupno"),
                          xw.Sheet("Info & <beleške>", [xw.Column("A"), xw.Column("B")], [["č ć š đ ž", "x<y"]])])


def test_zip_parts_are_well_formed_xml():
    z = zipfile.ZipFile(io.BytesIO(sample()))
    names = set(z.namelist())
    assert {"[Content_Types].xml", "xl/workbook.xml", "xl/styles.xml", "xl/worksheets/sheet1.xml",
            "xl/worksheets/sheet2.xml"} <= names
    for n in names:
        xml.dom.minidom.parseString(z.read(n))


def test_values_dates_totals():
    s = zipfile.ZipFile(io.BytesIO(sample())).read("xl/worksheets/sheet1.xml").decode()
    assert '<c r="A2" s="2"><v>46348</v></c>' in s          # 22.11.2026 as Excel serial date
    assert "<f>SUM(B2:B3)</f>" in s and "<f>SUM(C2:C3)</f>" in s
    assert 'state="frozen"' in s and '<autoFilter ref="A1:D3"/>' in s


def test_datetime_hours_are_exact():
    import openpyxl
    cols = [xw.Column("Sat", "datetime")]
    rows = [[dt.datetime(2026, 11, 22, h)] for h in range(24)]
    ws = openpyxl.load_workbook(io.BytesIO(xw.build_xlsx([xw.Sheet("S", cols, rows)])))["S"]
    assert [ws.cell(r, 1).value for r in range(2, 26)] == [r[0] for r in rows]


def test_col_letters():
    assert [xw.col_letter(i) for i in (0, 25, 26, 27, 701, 702)] == ["A", "Z", "AA", "AB", "ZZ", "AAA"]


def test_opens_with_openpyxl_if_available():
    try:
        import openpyxl
    except ImportError:
        return
    wb = openpyxl.load_workbook(io.BytesIO(sample()))
    ws = wb["Po danima"]
    assert ws["A2"].value.date() == dt.date(2026, 11, 22) and ws["B3"].value == 2.35
    assert ws["A4"].value == "Ukupno" and ws["B4"].value == "=SUM(B2:B3)"
    assert wb["Info & <beleške>"]["A2"].value == "č ć š đ ž"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
