"""Install the loopback dashboard compatibility service without opening desktop UI."""
import os,plistlib,subprocess
from pathlib import Path
BASE=Path('/Users/egor/.hermes/team-browser-access')
label='ai.hermes.headless-viewer'
path=Path.home()/'Library/LaunchAgents'/f'{label}.plist'
if path.exists():raise SystemExit('Service already installed; inspect before updating.')
config=dict(Label=label,ProgramArguments=['/Users/egor/.hermes/hermes-agent/venv/bin/python',str(BASE/'headless_dashboard.py')],RunAtLoad=True,KeepAlive=True,ThrottleInterval=30,StandardOutPath=str(BASE/'viewer-service.log'),StandardErrorPath=str(BASE/'viewer-service.log'))
path.write_bytes(plistlib.dumps(config));path.chmod(0o600)
subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True)
print('Installed loopback viewer on localhost:4850')
