"""Install narrow browser_tool integration; preserve other Hermes modifications."""
from pathlib import Path
import shutil
import time


def transform(source):
    old_path = 'return [hermes_node_bin, hermes_node_root, hermes_nm_bin, *list(_discover_homebrew_node_dirs()), *_SANE_PATH_DIRS]'
    new_path = 'return [hermes_node_bin, hermes_node_root, hermes_nm_bin, str(Path.home() / ".hermes/node/bin"), *list(_discover_homebrew_node_dirs()), *_SANE_PATH_DIRS]'
    source = source.replace(old_path, new_path)
    detector = '    # 1. AGENT_BROWSER_EXECUTABLE_PATH — explicit user-configured browser'
    if '# PM agent-browser native cache' not in source:
        source = source.replace(detector, '''    # PM agent-browser native cache (0.26 installer uses this on macOS).
    if sys.platform == "darwin":
        cache = Path.home() / ".agent-browser/browsers"
        if any(p.is_file() for p in cache.glob("chrome-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")):
            _cached_chromium_installed = True
            return True

''' + detector)
    if '# PM TEAM HEADLESS' in source:
        return source
    old = '    return {\n        "session_name": session_name,\n        "bb_session_id": None,\n        "cdp_url": None,\n        "features": {"local": True},\n    }'
    new = '''    # PM TEAM HEADLESS: persistent profile and lease per agent.
    from hermes_cli.config import read_raw_config
    from tools.team_headless import attach
    return attach({
        "session_name": session_name,
        "bb_session_id": None,
        "cdp_url": None,
        "features": {"local": True},
    }, task_id, get_hermes_home(),
       read_raw_config().get("browser", {}).get("team_headless", False))'''
    assert source.count(old) == 1
    source = source.replace(old, new)
    old = '        backend_args = ["--session", session_info["session_name"]]'
    assert source.count(old) == 1
    source = source.replace(old, old + '\n        from tools.team_headless import flags\n        backend_args += flags(session_info)')
    start = source.index('def _run_browser_command(')
    end = source.index('\ndef ', start + 5)
    fragment = source[start:end]
    pos = fragment.rindex('    return result')
    fragment = fragment[:pos] + '''    from tools.team_headless import record
    record(session_info, command, result, locals().get("task_socket_dir", ""))
''' + fragment[pos:]
    source = source[:start] + fragment + source[end:]
    old = '        logger.debug("Removed task %s from active sessions", task_id)'
    assert source.count(old) == 1
    source = source.replace(old, '        from tools.team_headless import release\n        release(session_info)\n' + old)
    return source


if __name__ == '__main__':
    root = Path('/Users/egor/.hermes/hermes-agent')
    target = root / 'tools/browser_tool.py'
    source = target.read_text()
    updated = transform(source)
    compile(updated, str(target), 'exec')
    backup = Path('/Users/egor/.hermes/team-browser-access') / f'backup-headless-{int(time.time())}'
    backup.mkdir()
    shutil.copy2(target, backup / target.name)
    shutil.copy2(Path(__file__).with_name('team_headless.py'), root / 'tools/team_headless.py')
    target.write_text(updated)
    import yaml
    base=root.parent
    for home in [base]+[p for p in (base/'profiles').iterdir() if (p/'config.yaml').exists() and not p.name.startswith('headless_smoke_')]:
        config=home/'config.yaml'
        name='default' if home==base else home.name
        shutil.copy2(config,backup/(name+'.yaml'))
        data=yaml.safe_load(config.read_text())
        data.setdefault('browser',{}).update(cloud_provider='local',use_gateway=False,
                                              team_headless=True,engine='chrome',cdp_url='')
        agent=data.setdefault('agent',{})
        agent['disabled_toolsets']=[t for t in agent.get('disabled_toolsets',[]) if t!='browser']
        config.write_text(yaml.safe_dump(data,allow_unicode=True,sort_keys=False))
        config.chmod(0o600)
    print(backup)
