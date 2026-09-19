from autobot.market_coverage import annotate_coverage


def test_every_row_has_one_explained_outcome_without_inventing_cost():
    rows = [
        {'type_slug': 'aggregate'},
        {'can_auto_price': False, 'requirements': {'issues': ['Единица неизвестна']}},
        {'can_auto_price': True, 'verified_count': 2, 'market_unit': 15.7},
        {'can_auto_price': True, 'candidate_count': 1},
        {'can_auto_price': True, 'market_processed': True, 'market_status': 'Сайт показал CAPTCHA'},
        {'can_auto_price': True, 'market_processed': True},
        {'can_auto_price': True},
    ]
    coverage = annotate_coverage(rows)
    assert coverage['total'] == 7
    assert all(coverage[key] == 1 for key in coverage if key != 'total')
    assert all(row['price_reason'] for row in rows)
    assert 'Единица неизвестна' == rows[1]['price_reason']
    assert rows[2]['market_unit'] == 15.7
    assert all('market_unit' not in row for row in rows[:2] + rows[3:])
