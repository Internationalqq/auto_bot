"""
Парсинг Excel «Приложение №2 к извещению (Обоснование НМЦК)» — таблица позиций
с коммерческими предложениями по датам, средними и блоком НМЦК.

Используется для выгрузки в JSON (в т.ч. дальнейший разбор / Алиса).
"""
from __future__ import annotations

import math
import re
from io import BytesIO
from pathlib import Path
from typing import Any

import pandas as pd

_MAX_HEADER_SCAN = 45
_MAX_COLS = 64


def _clean_header(s: str) -> str:
    t = re.sub(r"\s+", " ", (s or "").strip())
    return t


def _cell_json(v: object) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if pd.isna(v):
        return None
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            return str(v)
    if isinstance(v, (int,)):
        return int(v)
    if isinstance(v, float):
        if float(v).is_integer():
            return int(v)
        return float(v)
    return str(v).strip() if isinstance(v, str) else v


def _find_header_row(df: pd.DataFrame) -> int | None:
    n = min(len(df), _MAX_HEADER_SCAN)
    for i in range(n):
        row = df.iloc[i]
        for val in row:
            if not isinstance(val, str):
                continue
            low = val.lower()
            if "наименование" not in low:
                continue
            if any(
                x in low
                for x in ("работ", "товар", "услуг", "объём", "объем", "предмет")
            ):
                return int(i)
    return None


def _build_columns(df: pd.DataFrame, header_row: int) -> tuple[list[str], int]:
    """Имена колонок из двух строк заголовка; первая строка данных = header_row + 2."""
    nrows, ncols = df.shape
    ncols = min(ncols, _MAX_COLS)
    sub_row = header_row + 1
    names: list[str] = []
    used: dict[str, int] = {}
    for j in range(ncols):
        top = df.iat[header_row, j] if header_row < nrows else None
        bot = df.iat[sub_row, j] if sub_row < nrows else None
        t = "" if top is None or (isinstance(top, float) and pd.isna(top)) else str(top).strip()
        b = "" if bot is None or (isinstance(bot, float) and pd.isna(bot)) else str(bot).strip()
        if b:
            name = _clean_header(b)
        elif t:
            name = _clean_header(t)
        else:
            name = f"column_{j + 1}"
        base = name
        if base in used:
            used[base] += 1
            name = f"{base} ({used[base]})"
        else:
            used[base] = 0
        names.append(name)
    return names, sub_row + 1


def _is_position_row(row: pd.Series) -> bool:
    if len(row) < 2:
        return False
    no = row.iloc[0]
    name = row.iloc[1]
    if pd.isna(name) or (isinstance(name, str) and not name.strip()):
        return False
    if pd.isna(no):
        return False
    try:
        n = float(no)
    except (TypeError, ValueError):
        return False
    if n < 1 or math.isnan(n) or math.isinf(n):
        return False
    if abs(n - round(n)) > 1e-6:
        return False
    return True


def parse_nmck_justification_excel(
    source: Path | bytes,
    *,
    sheet_name: int | str = 0,
    original_name: str = "",
) -> dict[str, Any]:
    """
    Возвращает dict:
      columns — список имён колонок;
      rows — список объектов {column: value};
      meta — имя файла, лист, число строк.
    """
    if isinstance(source, Path):
        data = source.read_bytes()
        path = source
        name = original_name or path.name
    else:
        data = source
        name = original_name or "upload.xlsx"
        path = None

    bio = BytesIO(data)
    xl = pd.ExcelFile(bio)
    sheets = xl.sheet_names
    if isinstance(sheet_name, int):
        sn = sheets[sheet_name] if 0 <= sheet_name < len(sheets) else sheets[0]
    else:
        sn = sheet_name if sheet_name in sheets else sheets[0]

    df = pd.read_excel(BytesIO(data), sheet_name=sn, header=None, dtype=object)
    header_row = _find_header_row(df)
    if header_row is None:
        for cand in sheets:
            if cand == sn:
                continue
            df2 = pd.read_excel(BytesIO(data), sheet_name=cand, header=None, dtype=object)
            hr = _find_header_row(df2)
            if hr is not None:
                df = df2
                sn = cand
                header_row = hr
                break
    if header_row is None:
        raise ValueError(
            "Не найдена строка заголовка с «Наименование» (типичное приложение №2 к извещению)."
        )

    columns, data_start = _build_columns(df, header_row)
    rows_out: list[dict[str, Any]] = []
    for i in range(data_start, len(df)):
        row = df.iloc[i]
        if not _is_position_row(row):
            continue
        rec: dict[str, Any] = {}
        for j, col in enumerate(columns):
            if j >= len(row):
                break
            rec[col] = _cell_json(row.iloc[j])
        rows_out.append(rec)

    meta = {
        "filename": name,
        "sheet": sn,
        "row_count": len(rows_out),
        "column_count": len(columns),
    }
    if path is not None:
        meta["path"] = str(path)
    return {"columns": columns, "rows": rows_out, "meta": meta}


def main() -> None:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="JSON из Excel обоснования НМЦК (приложение №2)")
    ap.add_argument("xlsx", type=Path, help="Путь к .xlsx")
    ap.add_argument("-o", "--out", type=Path, help="Записать JSON в файл")
    args = ap.parse_args()
    out = parse_nmck_justification_excel(args.xlsx)
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"OK -> {args.out} ({out['meta']['row_count']} строк)")
    else:
        print(text[:8000])
        if len(text) > 8000:
            print("\n... [усечено, укажите -o файл]")


if __name__ == "__main__":
    main()
