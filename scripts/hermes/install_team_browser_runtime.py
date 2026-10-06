"""Deploy operator-approved scoped recovery without replacing existing changes."""
import json
from pathlib import Path
import shutil
import time
import yaml

HERMES = Path('/Users/egor/.hermes/hermes-agent')
TEAM = Path('/Users/egor/.hermes/team-browser-access')


def replace_once(path, before, after, backup):
    source=path.read_text()
    if after in source:return
    if source.count(before)!=1:raise RuntimeError('Unexpected source contract: '+str(path))
    if not (backup/path.name).exists():shutil.copy2(path,backup/path.name)
    path.write_text(source.replace(before,after,1))


def main():
    backup=TEAM/('backup-recovery-'+str(int(time.time())))
    backup.mkdir(parents=True)
    shutil.copy2(Path(__file__).with_name('team_browser_runtime.py'),HERMES/'tools/computer_use/team_runtime.py')
    replace_once(HERMES/'cli.py',
        '        # Build a command-ish string so the existing UI renders something',
        '        from tools.computer_use.team_runtime import approval\n'
        '        scoped_verdict = approval(action, args)\n'
        '        if scoped_verdict is not None:\n'
        '            return scoped_verdict\n'
        '        # Build a command-ish string so the existing UI renders something',backup)
    replace_once(HERMES/'tools/computer_use/tool.py',
        '    if verdict == "approve_once":\n',
        '    if isinstance(verdict, str) and verdict.startswith("deny:"):\n'
        '        return json.dumps({"error": "approval_required", "reason": verdict[5:], "action": action})\n'
        '    if verdict == "approve_once":\n',backup)
    replace_once(HERMES/'tools/computer_use/tool.py',
        '        cap = backend.capture(mode=mode, app=args.get("app"))\n',
        '        try:\n'
        '            cap = backend.capture(mode=mode, app=args.get("app"))\n'
        '        except RuntimeError as exc:\n'
        '            from tools.computer_use.team_runtime import policy_context, retryable_window_error, recover_browser\n'
        '            if not (policy_context(args.get("app")) and retryable_window_error(exc)):\n'
        '                raise\n'
        '            recover_browser(backend, args["app"])\n'
        '            cap = backend.capture(mode=mode, app=args["app"])\n',backup)
    replace_once(HERMES/'tools/computer_use/tool.py',
        '        res = backend.focus_app(app, raise_window=bool(args.get("raise_window")))\n',
        '        res = backend.focus_app(app, raise_window=bool(args.get("raise_window")))\n'
        '        if not res.ok and args.get("raise_window"):\n'
        '            from tools.computer_use.team_runtime import policy_context, retryable_window_error, recover_browser\n'
        '            if policy_context(app) and retryable_window_error(res.message):\n'
        '                recover_browser(backend, app)\n'
        '                res = ActionResult(ok=True, action="focus_app", message="Browser recovered; capture again before any input.")\n',backup)
    # Retain WindowServer metadata. Prefer currently visible windows over stale
    # hidden overlays when explicitly requesting activation, without skipping
    # visible dialogs. Existing window identity checks still apply.
    path=HERMES/'tools/computer_use/cua_backend.py'
    source=path.read_text()
    start=source.index('    def focus_app(')
    end=source.index('    # ── App lifecycle',start)
    segment=source[start:end]
    old='                "z_index": w.get("z_index", 0),\n'
    new=old+'                "is_on_screen": w.get("is_on_screen", False),\n                "title": w.get("title", ""),\n                "bounds": w.get("bounds", {}),\n'
    if '"is_on_screen": w.get' not in segment:
        if segment.count(old)!=1:raise RuntimeError('Focus source contract changed')
        segment=segment.replace(old,new,1)
        segment=segment.replace('        matched.sort(key=lambda w: w["window_id"] != previous_window)',
            '        visible = [w for w in matched if w["is_on_screen"]]\n'
            '        if visible:\n'
            '            matched = visible\n'
            '        matched.sort(key=lambda w: w["window_id"] != previous_window)')
        shutil.copy2(path,backup/path.name)
        path.write_text(source[:start]+segment+source[end:])
    profiles={}
    for name in ['default','gulya','commercial','bot1','pto','anya','secretary_albina','dima','vika','rustam','zakup_alibert','dasha','vk_worker']:
        home=Path('/Users/egor/.hermes') if name=='default' else Path('/Users/egor/.hermes/profiles')/name
        config=home/'config.yaml'
        if not config.exists():continue
        c=yaml.safe_load(config.read_text())
        apps=c.get('computer_use',{}).get('allowed_apps') or []
        # Do not add apps: preserve existing assignment, narrowed to working UI.
        profiles[name]=[a for a in apps if a in {'Firefox','Google Chrome','Safari','Telegram'}]
    policy=TEAM/'unattended-policy.json'
    if policy.exists():shutil.copy2(policy,backup/policy.name)
    policy.write_text(json.dumps({'authorized_at':'2026-10-06','profiles':profiles,
        'scope':'Assigned browser operations and graceful recovery for existing authorized tasks; shared lock required. No new messaging authorization.'},ensure_ascii=False,indent=2))
    policy.chmod(0o600)
    print(json.dumps({'backup':str(backup),'profiles':profiles}))


if __name__=='__main__':main()
