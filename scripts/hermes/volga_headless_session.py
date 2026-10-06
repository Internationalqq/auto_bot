"""One existing Volga position through native, invisible browser tools."""
import json
import os
from pathlib import Path
import re
import sys
import time


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
        out=original(task_id,command,args,**kwargs)
        with (batch/'approval-audit.jsonl').open('a') as log:
            log.write(json.dumps(dict(at=time.time(),action=command,backend='headless',
                                     success=out.get('success'),verdict='observed'))+'\n')
        return out
    browser._run_browser_command=audited
    (batch/'runtime-model.json').write_text(json.dumps(dict(model=expected['model'],
        reasoning={'effort':expected['reasoning_effort']},browser='local-headless')))
    try:
        cli.main(query=(batch/'prompt.txt').read_text(),quiet=True,toolsets='volga-headless',max_turns=60)
    finally:
        browser.cleanup_all_browsers()


if __name__=='__main__':main()
