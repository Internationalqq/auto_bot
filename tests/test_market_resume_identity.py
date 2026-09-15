import json
from datetime import datetime, timezone
import pandas as pd
from autobot.market_contract import BUNDLE_COLUMN, position_identity
from autobot.market_analytics import COL_NAME, COL_UNIT, COL_QTY, COL_UNIT_PRICE, COL_SUM
from autobot.real_market_scraper import _merge_rows, _processed_keys, _resolve_agent_source_row
import pytest


def row(unit='м3', price=2500, verification='verified', source='a.xlsx'):
    return {COL_NAME: 'Щебень гранитный', COL_UNIT: unit, COL_QTY: 1, COL_UNIT_PRICE: 3000,
            COL_SUM: 3000, 'Файл ЛСР': source, BUNDLE_COLUMN: json.dumps([{'price': price,
            'url': 'https://supplier.example/item', 'verification': verification,
            'matched_unit': unit, 'observed_at': datetime.now(timezone.utc).isoformat()}])}


def test_rerun_one_unit_preserves_other_unit_and_other_file():
    a, b, c = row(), row(unit='т'), row(source='c.xlsx')
    result = _merge_rows(pd.DataFrame([a, b, c]), [row(price=2600)])
    assert len(result) == 3
    assert set(result[COL_UNIT]) == {'м3', 'т'}
    assert len(result[result['Файл ЛСР'] == 'c.xlsx']) == 1
    assert json.loads(result.iloc[-1][BUNDLE_COLUMN])[0]['price'] == 2600


def test_failed_search_is_not_marked_complete_and_retry_keeps_verified_rows():
    good, empty = row(), row(unit='т', verification='candidate')
    processed = _processed_keys(pd.DataFrame([good, empty]))
    assert processed == {position_identity(good)}
    assert position_identity(empty) not in processed


def test_changed_search_region_does_not_skip_previous_local_price():
    saved = row()
    saved['Регион поиска'] = 'Ярославль'
    offers = json.loads(saved[BUNDLE_COLUMN])
    offers[0].update(search_region='Ярославль', region_evidence='Доставка по Ярославлю')
    saved[BUNDLE_COLUMN] = json.dumps(offers)
    frame = pd.DataFrame([saved])
    assert _processed_keys(frame, region='Ярославль') == {position_identity(saved)}
    assert _processed_keys(frame, region='Миасс') == set()


def test_legacy_price_without_unit_cannot_be_assigned_by_title_alone():
    from autobot.market_contract import merge_market_frames, confirmed_prices

    legacy = row()
    legacy.pop(COL_UNIT)
    merged = merge_market_frames(pd.DataFrame([row()]), pd.DataFrame([legacy]))
    assert confirmed_prices(merged.iloc[0]) == []
    assert json.loads(merged.iloc[0][BUNDLE_COLUMN])[0]['verification'] == 'candidate'


def test_worker_result_uses_exact_position_and_rejects_changed_input():
    cubic, tonne = row(), row(unit='т')
    frame = pd.DataFrame([cubic, tonne])
    resolved = _resolve_agent_source_row(frame, {'name': cubic[COL_NAME], 'position_key': position_identity(tonne)})
    assert resolved[COL_UNIT] == 'т'
    with pytest.raises(ValueError, match='изменилась'):
        changed = dict(tonne, **{COL_QTY: 2})
        _resolve_agent_source_row(pd.DataFrame([cubic, changed]), {'position_key': position_identity(tonne)})
    with pytest.raises(ValueError, match='неоднозначна'):
        _resolve_agent_source_row(frame, {'name': cubic[COL_NAME]})


def test_resume_limit_applies_to_missing_prices_not_already_completed_rows(monkeypatch, tmp_path):
    from autobot import real_market_scraper as scraper

    good = row()
    pending = row(unit='т', verification='candidate')
    pending[COL_SUM] = 1000
    estimate = tmp_path / 'estimate.xlsx'
    output = tmp_path / 'market.xlsx'
    pd.DataFrame([good, pending]).to_excel(estimate, index=False)
    pd.DataFrame([good]).to_excel(output, index=False)
    monkeypatch.setattr(scraper, 'estimate_path_for_tender', lambda _: estimate)
    monkeypatch.setattr(scraper, 'output_path_for_estimate', lambda _: output)
    monkeypatch.setattr(scraper, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(scraper, 'load_tender_metadata', lambda: {})
    monkeypatch.setattr(scraper, '_revalidate_previous', lambda df: (df, 0))
    monkeypatch.setattr(scraper, 'append_market_web_event', lambda *a, **kw: None)

    scraper.run_tender('123456', max_rows=1, dry_run=True, pause=0)

    saved = pd.read_excel(output)
    assert len(saved) == 2
    assert saved.iloc[-1][COL_UNIT] == 'т'
    assert _processed_keys(saved) == {position_identity(good)}
