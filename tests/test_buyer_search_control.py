"""20 real Volga estimate rows; deterministic qualification, no live quotes.

Public HTML fragments below are test fixtures, not supplier quotations. Live
measurements belong to the release report and never become test expectations.
"""
import html
import json
from pathlib import Path
import unittest

from autobot import buyer_needs as needs, buyer_discovery as discovery
from autobot.buyer_replies import comparison_amount
from autobot.market_source_adapters import inspect_source_page

CONTROL=json.loads((Path(__file__).parent/'fixtures'/'volga_discovery_control.json').read_text(encoding='utf-8'))


class SearchControlTests(unittest.TestCase):
    def test_twenty_real_rows_preserve_procurement_identity_and_exclude_adjustment(self):
        self.assertEqual(len(CONTROL['positions']),20)
        source=needs.snapshot(CONTROL)
        self.assertEqual(len(source['positions']),19)
        self.assertEqual(len(source['rejected']),1)
        queries=needs.queries(source)
        keys={p['position_key'] for p in source['positions']}
        self.assertEqual({k for q in queries for k in q['position_keys']},keys)
        self.assertTrue(all(set(q['position_keys'])<=keys for q in queries))
        for row in source['positions']:
            related=[q for q in queries if row['position_key'] in q['position_keys']]
            with self.subTest(name=row['name']):
                if row['type_slug']=='material':
                    product=next(q for q in related if q['intent']=='product')
                    for model in needs.product_identifiers(row['name']):
                        self.assertIn(model,product['query']);self.assertIn(model,product['fallback_query'])
                else:self.assertTrue(all(q['bucket']=='works' for q in related))

    def test_wrong_currency_and_unknown_unit_never_become_prices_for_control_materials(self):
        for row in CONTROL['positions']:
            if row['type_slug']!='material':continue
            for currency,unit in [('USD',row['unit']),('RUB','')]:
                product={'@type':'Product','name':row['name'],'offers':{'@type':'Offer','price':100,'priceCurrency':currency,'unitText':unit}}
                page='<h1>'+html.escape(row['name'])+'</h1><script type="application/ld+json">'+json.dumps(product)+'</script>'
                with self.subTest(name=row['name'],currency=currency,unit=unit):
                    result=inspect_source_page(page,'https://supplier.example/card',name=row['name'],target_unit=row['unit'],position_bucket='materials')
                    self.assertFalse(result.accepted,result)

    def test_wrong_fraction_geogrid_height_and_cable_variant_are_rejected(self):
        cases=[('Щебень', 'Щебень М1200 фракция 5-40 мм', 'м3'),
               ('Георешетка', 'Георешетка 210х210 мм высота 50 мм 80/15 кН/м', 'м2'),
               ('Кабель АВБбШв', 'Кабель АВБШв 4х150 1кВ', 'м')]
        for prefix,offer,unit in cases:
            row=next(r for r in CONTROL['positions'] if r['name'].startswith(prefix))
            page='<h1>'+offer+'</h1><p>'+offer+' — 100 руб/'+unit+'</p>'
            with self.subTest(name=row['name']):
                self.assertFalse(inspect_source_page(page,'https://supplier.example/card',name=row['name'],target_unit=row['unit'],position_bucket='materials').accepted)

    def test_piece_price_is_not_treated_as_price_for_hundred_pieces(self):
        row=next(r for r in CONTROL['positions'] if 'У733М' in r['name'])
        self.assertEqual(comparison_amount(9120,'шт',row['unit']),912000)

    def test_each_missing_price_retains_source_and_reason(self):
        row=next(r for r in CONTROL['positions'] if '4ПКТп' in r['name'])
        source=needs.snapshot({**CONTROL,'positions':[row]})
        task=next(q for q in needs.queries(source) if q['intent']=='product')|{'url':'https://supplier.example/card'}
        page='<h1>'+row['name']+'</h1><p>Купить товар. Цена по запросу.</p><a href="mailto:info@supplier.example">Email</a>'
        result=discovery.inspect(task,source,fetch=lambda url:(url,page))
        self.assertEqual(result['prices'],[])
        self.assertEqual(result['price_checks'][0]['position_key'],row['position_key'])
        self.assertEqual(result['price_checks'][0]['source_url'],task['url'])
        self.assertFalse(result['price_checks'][0]['accepted'])
        self.assertTrue(result['price_checks'][0]['reason'])
