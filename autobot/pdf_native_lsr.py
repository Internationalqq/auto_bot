"""Read a text-layer, twelve-column LSR using its printed column boundaries.

Unsupported or mixed scan layouts return None so the existing OCR path can run.
Position totals are read on their own labelled line, never near the next price.
"""
from __future__ import annotations

import io
import re
from decimal import Decimal, InvalidOperation


class UnsupportedLayout(ValueError):
    pass


def clean(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def number(value):
    value=clean(value).replace(' ', '').replace(',', '.')
    if not re.fullmatch(r'-?\d+(?:\.\d+)?', value): return None
    try: return Decimal(value)
    except InvalidOperation: return None


def _header(page):
    settings={'vertical_strategy':'lines','horizontal_strategy':'lines',
              'snap_tolerance':5,'join_tolerance':5,'intersection_tolerance':8}
    for table in page.find_tables(settings):
        for row, cells in zip(table.extract(), table.rows):
            if [clean(value) for value in row] != list(map(str,range(1,13))): continue
            if any(cell is None for cell in cells.cells): continue
            bounds=[cells.cells[0][0], *[cell[2] for cell in cells.cells]]
            if any(right<=left for left,right in zip(bounds,bounds[1:])): continue
            return bounds,max(cell[3] for cell in cells.cells)
    return None


def _lines(page, bounds, top):
    words=[word for word in page.extract_words(x_tolerance=1.5,y_tolerance=2)
           if word['top']>=top-1 and bounds[0]-1<=word['x0']<bounds[-1]]
    groups=[]
    for word in sorted(words,key=lambda w:((w['top']+w['bottom'])/2,w['x0'])):
        center=(word['top']+word['bottom'])/2
        if not groups or abs(center-groups[-1][0])>2.5:
            groups.append((center,[]))
        groups[-1][1].append(word)
    result=[]
    for center,group in groups:
        cells=[[] for _ in range(12)]
        for word in sorted(group,key=lambda w:w['x0']):
            for column,(left,right) in enumerate(zip(bounds,bounds[1:])):
                if left-1<=word['x0']<right-1:
                    cells[column].append(word['text']); break
        result.append({'y':center,'cells':[clean(' '.join(cell)) for cell in cells]})
    return result


def _code(value):
    return bool(re.match(r'^(?:[А-ЯЁA-Z]{1,10}[а-яa-z]?[-_\d]|\d{2}[.-]\d|\d{2,4}/пр)',value)
                and re.search(r'\d',value))


def records_from_lines(lines):
    """Process physical cell lines, including positions split over page breaks."""
    anchors=[]; used=set()
    for index,line in enumerate(lines):
        value=line['cells'][0]
        if not re.fullmatch(r'\d{1,4}(?:\.\d{1,3})?',value): continue
        nearby=[j for j in range(max(0,index-3),min(len(lines),index+2))
                if lines[j]['page']==line['page'] and abs(lines[j]['y']-line['y'])<=25
                and _code(lines[j]['cells'][1])]
        if not nearby: raise UnsupportedLayout('Numbered row has no readable basis code: '+value)
        start=min(nearby,key=lambda j:(abs(lines[j]['y']-line['y']),j>index))
        if start in used: raise UnsupportedLayout('Two positions share one basis line')
        used.add(start)
        anchors.append({'start':start,'position':value,'page':line['page']})
    anchors.sort(key=lambda a:a['start'])
    if not anchors: return []
    rows=[]; section=''
    for i,anchor in enumerate(anchors):
        start=anchor['start']
        end=anchors[i+1]['start'] if i+1<len(anchors) else len(lines)
        first=lines[start]['cells']
        for line in lines[anchors[i-1]['start'] if i else 0:start]:
            text=clean(' '.join(line['cells'][:3]))
            if re.match(r'^Раздел\s+\d+',text,re.I): section=text
        name=[]
        for line in lines[start:end]:
            cells=line['cells']; text=cells[2]
            if name and (re.match(r'^(?:Об[ъь]?[её]м\s*[=:]|Всего\b|Итого\b|ФОТ\b|ОТм?\b|ЭМ\b|'
                                  r'НР\b|СП\b|[1-5]\s+(?:ОТ|ЭМ|М|ОБ)\b)',text,re.I)
                         or (cells[1] and line is not lines[start] and re.match(r'^(?:Пр/|\d+-\d+-)',cells[1],re.I))):
                break
            if text: name.append(text)
        title=clean(' '.join(name))
        title=re.sub(r'(?<=[A-Za-z0-9])-\s+(?=[A-Za-z0-9])','-',title)
        if not title: raise UnsupportedLayout('Empty position description')
        basis=first[1]
        if basis.startswith('ТЦ_'):
            for line in lines[start+1:end]:
                tail=line['cells'][1]
                if not tail: continue
                if re.fullmatch(r'[\d._]+',tail): basis+=tail
                else: break
        # The unit and final quantity belong to the first physical row. A
        # resource or machine rate further below cannot supply a missing value.
        unit=clean(first[3]); qty=number(first[6]); direct=number(first[11])
        is_resource='.' in anchor['position']
        primary_end=next((a['start'] for a in anchors[i+1:] if '.' not in a['position']),len(lines))
        totals=[number(line['cells'][11]) for line in lines[start:primary_end]
                if re.match(r'^Всего\s+по\s+позиции\b',clean(' '.join(line['cells'][:3])),re.I)]
        totals=[value for value in totals if value is not None]
        if not is_resource and len(set(totals))>1:
            raise UnsupportedLayout('Ambiguous total for one position')
        total=direct if is_resource else totals[0] if totals else direct
        price=total/qty if total is not None and qty else number(first[9])
        rows.append({'position_id':f"pdf:native:{anchor['page']}:{anchor['position']}",
                     'parent_position_id':'','page':anchor['page'],'position':anchor['position'],
                     'code':basis,'name':title,'unit':unit,
                     'qty':float(qty) if qty is not None else None,
                     'total':float(total) if total is not None else None,
                     'unit_price':float(price) if price is not None else None,
                     'section':section,'extract_source':'PDF text LSR'})
    parents={}; primary=[]
    for row in rows:
        if '.' in row['position']:
            parent=parents.get(row['position'].split('.')[0])
            if parent is None: raise UnsupportedLayout('Resource has no preceding parent')
            row['parent_position_id']=parent['position_id']
            parent.setdefault('resources',[]).append(row)
        else:
            if row['position'] in parents: raise UnsupportedLayout('Repeated primary position number')
            parents[row['position']]=row; primary.append(row)
    return primary


def read_native_lsr(raw):
    try:
        import pdfplumber
        from pdfplumber.utils.exceptions import PdfminerException
    except ImportError: return None
    lines=[]; seen_table=False
    try:
        with pdfplumber.open(io.BytesIO(raw)) as document:
            for page_number,page in enumerate(document.pages,1):
                # Do not publish just the text-bearing pages of a mixed PDF.
                if len(page.chars)<80 and any(
                        (image['x1']-image['x0'])*(image['bottom']-image['top'])>page.width*page.height*.4
                        for image in page.images):
                    return None
                header=_header(page)
                if header is None:
                    if seen_table and len(page.chars)>120: return None
                    continue
                seen_table=True
                for line in _lines(page,*header):
                    line['page']=page_number
                    lines.append(line)
        return records_from_lines(lines) or None
    except (UnsupportedLayout,ValueError,KeyError,TypeError,PdfminerException):
        return None


def read_native_total(raw):
    """Read the printed total in its own physical cell, not text-stream order."""
    try:
        import pdfplumber
        from pdfplumber.utils.exceptions import PdfminerException
    except ImportError:
        return None
    try:
        values=[]
        with pdfplumber.open(io.BytesIO(raw)) as document:
            for page in document.pages[-3:]:
                header=_header(page)
                if header is None:
                    continue
                for line in _lines(page,*header):
                    cells=line['cells']
                    if re.fullmatch(r'(?:ВСЕГО|ИТОГО)\s+по\s+смете',clean(' '.join(cells[:3])),re.I):
                        value=number(cells[11])
                        if value is not None and value>0:
                            values.append(value)
        return float(values[0]) if values and len(set(values))==1 else None
    except (ValueError,KeyError,TypeError,PdfminerException):
        return None
