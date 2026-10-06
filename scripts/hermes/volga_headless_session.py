"""One existing Volga position through native, invisible browser tools."""
import json
import os
from pathlib import Path
import re
import struct
import sys
import time


def access_challenge(out):
    """Recognize explicit human checks, not ordinary supplier-page failures."""
    text = json.dumps(out, ensure_ascii=False).lower()
    return any(s in text for s in (
        'google.com/sorry', 'our systems have detected unusual traffic',
        'verify you are human', 'подтвердите, что вы не робот',
        'подтвердите, что вы человек', 'с вашей сети поступают необычные запросы'))


def viewport_capture(original, task_id, command, args=None, **kwargs):
    """Bound only this run's browser screenshots before native vision encodes them.

    browser_vision otherwise always requests --full, including arbitrarily tall
    pages. Keep readable viewport pixels; scroll for the rest. If the driver ever
    ignores viewport mode, return a normal tool error rather than poison the
    model conversation with an oversized image. Original artifacts are preserved.
    """
    if command != 'screenshot':
        return original(task_id, command, args, **kwargs)
    capture_args = [a for a in (args or []) if a not in ('--full', '--full-page')]
    out = original(task_id, command, capture_args, **kwargs)
    if not out.get('success'):
        return out
    try:
        path = Path(out.get('data', {}).get('path') or capture_args[-1])
        with path.open('rb') as f:
            header = f.read(24)
        if (len(header) != 24 or header[:8] != b'\x89PNG\r\n\x1a\n'
                or header[12:16] != b'IHDR'):
            raise ValueError('Expected a PNG browser screenshot')
        width, height = struct.unpack('>II', header[16:24])
        if (not width or not height or max(width, height) > 2048
                or width * height > 3_000_000 or path.stat().st_size > 8_000_000):
            raise ValueError(f'Viewport image is too large: {width}x{height}')
    except (OSError, ValueError, IndexError, TypeError) as exc:
        return {'success':False, 'error':f'Image not sent to model: {exc}. '
                'Use browser_snapshot and browser_scroll to read the page; '
                'do not retry a full-page image.'}
    return dict(out, capture_mode='viewport', image_dimensions=[width, height])


def main():
    batch = Path(sys.argv[1]).resolve()
    root = batch.parent
    state = json.loads((root/'run-state.json').read_text())
    assert state['started_at'] <= time.time() < state['deadline']
    source = json.loads((root/'input.json').read_text())['source']
    assert source['tender_id'] == '0171200001926000664'
    match = re.fullmatch(r'batch-([1-9][0-9]*)(?:-attempt-2)?', batch.name)
    assert match
    assert json.loads((batch/'positions.json').read_text()) == [source['positions'][int(match[1])-1]]
    assert os.environ['HERMES_HOME']=='/Users/egor/.hermes/profiles/commercial'
    sys.path.insert(0,'/Users/egor/.hermes/hermes-agent')
    import cli
    import toolsets
    from hermes_cli.config import load_config
    configured=load_config()
    expected=json.loads((root/'expected-model.json').read_text())
    assert configured['model']['default']==expected['model']
    assert cli.CLI_CONFIG['agent']['reasoning_effort']==expected['reasoning_effort']
    assert configured['browser']['team_headless']
    assert configured['browser']['cloud_provider']=='local'
    toolsets.TOOLSETS['volga-headless'] = dict(description='Public price research',includes=['file'],
        tools=['browser_navigate','browser_snapshot','browser_click','browser_type',
               'browser_scroll','browser_back','browser_press','browser_get_images','browser_vision'])
    from model_tools import get_tool_definitions
    names={t['function']['name'] for t in get_tool_definitions(
        enabled_toolsets=['volga-headless'], disabled_toolsets=configured['agent'].get('disabled_toolsets'),
        quiet_mode=True, skip_tool_search_assembly=True)}
    assert {'browser_navigate','browser_snapshot','browser_click','browser_type'} <= names, 'Headless tools missing; stop before any model call'
    from tools import browser_tool as browser
    original=browser._run_browser_command
    def audited(task_id, command, args=None, **kwargs):
        if time.time() >= state['deadline']:
            return {'success':False,'error':'Run deadline reached'}
        if (batch/'access-challenge.json').exists():
            return {'success':False,'error':'Human verification required. Save current evidence and stop; do not retry or switch browsers.'}
        out=viewport_capture(original,task_id,command,args,**kwargs)
        if access_challenge(out):
            (batch/'access-challenge.json').write_text(json.dumps(
                dict(at=time.time(), reason='human_verification_required', command=command)))
            out={'success':False,'error':'Human verification required. Save partial result and stop. Do not switch browser/profile or retry the request.'}
        with (batch/'approval-audit.jsonl').open('a') as log:
            log.write(json.dumps(dict(at=time.time(),action=command,backend='headless',
                                     success=out.get('success'),verdict='observed',
                                     capture_mode=out.get('capture_mode'),
                                     image_dimensions=out.get('image_dimensions')))+'\n')
        return out
    browser._run_browser_command=audited
    (batch/'runtime-model.json').write_text(json.dumps(dict(model=expected['model'],
        reasoning={'effort':expected['reasoning_effort']},browser='local-headless')))
    try:
        vision_note = ('\nBrowser vision captures only the current viewport, not the whole page. '
                       'For long price lists use browser_snapshot and browser_scroll, then '
                       'capture again when needed. A screenshot-size tool error is not a '
                       'site access denial; continue using text snapshots.\n')
        cli.main(query=(batch/'prompt.txt').read_text()+vision_note,quiet=True,
                 toolsets='volga-headless',max_turns=60)
    finally:
        browser.cleanup_all_browsers()


if __name__=='__main__':main()
