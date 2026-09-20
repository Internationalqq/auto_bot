"""Search explicit LSR resources without counting them twice in its budget.

The authoritative report keeps one row per primary position. Its embedded
resource records are covered by the same report digest and publication lock.
"""
import json
from decimal import Decimal

import pandas as pd

from autobot.market_analytics import COL_NAME, COL_QTY, COL_SUM, COL_UNIT, COL_UNIT_PRICE
from autobot.market_contract import clean, decimal_number, kopecks, BUNDLE_COLUMN

RESOURCES = 'Ресурсы позиции (json)'
PARENT = 'parent_position_id'


def expand_resources(frame):
    if RESOURCES not in frame.columns or PARENT in frame.columns:
        return frame.copy()
    result=[]
    for row in frame.to_dict('records'):
        raw=clean(row.get(RESOURCES))
        resources=json.loads(raw) if raw else []
        if not isinstance(resources,list) or len(resources)>1000:
            raise ValueError('Некорректный состав ресурсов сметы')
        parent=clean(row.get('position_id'))
        row[PARENT]=''
        row['has_resources']=bool(resources)
        result.append(row)
        seen=set()
        for resource in resources:
            if (not isinstance(resource,dict) or not parent or
                    resource.get('parent_position_id')!=parent or
                    not resource.get('position_id') or resource['position_id'] in seen):
                raise ValueError('Не удалось однозначно связать ресурс с позицией сметы')
            seen.add(resource['position_id'])
            child={**row, RESOURCES:'', PARENT:parent,'has_resources':False,
                'parent_item_no':clean(row.get('№ п/п')), 'position_id':resource['position_id'],
                '№ п/п':resource.get('position'), 'basis_code':resource.get('code'),
                COL_NAME:resource.get('name'),COL_UNIT:resource.get('unit'),
                COL_QTY:resource.get('qty'),COL_UNIT_PRICE:resource.get('unit_price'),
                COL_SUM:resource.get('total'), 'Объем':'',
                'Лист':f"PDF, стр. {resource['page']}"}
            result.append(child)
    return pd.DataFrame(result,columns=list(dict.fromkeys([*frame.columns,PARENT,'has_resources','parent_item_no'])))


def financial_scope(frame):
    """Allocate each source rouble once; composite work prices stay incomplete.

An external labour rate does not price the machinery/overheads of a composite
LSR position. Its explicit materials can still contribute known market costs.
"""
    if PARENT not in frame.columns:
        return frame.copy()
    rows=frame.to_dict('records')
    groups={}
    for row in rows:
        if clean(row.get(PARENT)):
            groups.setdefault((clean(row.get('Файл ЛСР')),clean(row[PARENT])),[]).append(row)
    omitted=set()
    for row in rows:
        children=groups.get((clean(row.get('Файл ЛСР')),clean(row.get('position_id'))),[])
        if not children: continue
        amounts=[kopecks(child.get(COL_SUM)) for child in children]
        total=kopecks(row.get(COL_SUM))
        # No partial allocation when a resource amount is unreadable.
        if total is None or any(amount is None for amount in amounts):
            omitted.update(id(child) for child in children)
        else:
            residual=Decimal(total-sum(amounts))/100
            qty=decimal_number(row.get(COL_QTY))
            row[COL_SUM]=float(residual)
            row[COL_UNIT_PRICE]=float(residual/qty) if qty else None
        row[BUNDLE_COLUMN]='[]'
    return pd.DataFrame([row for row in rows if id(row) not in omitted])
