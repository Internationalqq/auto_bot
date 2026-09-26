"""Install the opt-in desktop queue in named Mac profiles, without restarting.

Backs up configs verbatim. Caller must check active work and perform a graceful
restart separately. No credentials are read from auth files or printed.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import yaml


def install(root, source, profiles, lock_path):
    if not lock_path.is_absolute():
        raise ValueError('Shared lock must be an absolute path')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    results = []
    for name in profiles:
        profile = (root/'profiles'/name).resolve()
        if profile.parent != (root/'profiles').resolve():
            raise ValueError('Invalid profile name')
        path = profile/'config.yaml'
        before = path.read_bytes()
        config = yaml.safe_load(before) or {}
        plugins = config.setdefault('plugins', {})
        disabled = plugins.get('disabled') or []
        if 'desktop-queue' in disabled:
            raise ValueError('desktop-queue explicitly disabled in ' + name)
        enabled = plugins.setdefault('enabled', [])
        if 'desktop-queue' not in enabled:
            enabled.append('desktop-queue')
        plugins.setdefault('entries', {}).setdefault('desktop-queue', {})['lock_path'] = str(lock_path)
        backup = profile/'backups'/('desktop-queue-' + stamp)
        backup.mkdir(parents=True, mode=0o700)
        shutil.copy2(path, backup/'config.yaml')
        (backup/'config.yaml').chmod(0o600)
        target = profile/'plugins/desktop-queue'
        if target.exists():
            shutil.copytree(target, backup/'plugin')
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        for filename in ('__init__.py', 'plugin.yaml'):
            shutil.copy2(source/filename, target/filename)
        after = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        pending = path.with_suffix('.desktop-queue.tmp')
        pending.write_text(after, encoding='utf-8')
        pending.chmod(0o600)
        pending.replace(path)
        results.append({'profile':name, 'backup':str(backup)})
    return results


if __name__ == '__main__':
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-home', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--lock-path', type=Path, required=True)
    parser.add_argument('--profile', action='append', required=True)
    args = parser.parse_args()
    print(json.dumps(install(args.hermes_home, args.source, args.profile, args.lock_path), indent=2))
