#!/usr/bin/env python3
"""Add Blackwire 3220 to Poly Lens Devices.config so it stops being classified as bricked."""
import sys
import json
from pathlib import Path

DEFAULT_APP = Path("/Applications/Poly Studio.app")
CONFIG_REL = Path("Contents/Helpers/LegacyHostApp.app/Contents/Resources/Devices.config")

app_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_APP
path = str(app_path / CONFIG_REL)
with open(path) as f:
    d = json.load(f)

# Remove any existing C056 entries to avoid duplicates
d['devices'] = [dev for dev in d['devices'] if dev.get('ProductID') != 'C056']

# Blackwire 3220 uses Conexant CX2070x — same handler family as Yeti (AB01/AB02/AB11/AB12)
d['devices'].append({
    'ProductID': 'C056',
    'usagePage': 'FFA0',
    'Name': 'Blackwire 3220',
    'DeviceEventHandler': 'YetiEvent',
    'HostCommandHandler': 'YetiCommand',
    'DeviceListenerHandler': '',
    'HostCommandDelay': ''
})

with open(path, 'w') as f:
    json.dump(d, f, indent=2)

print('Added Blackwire 3220 (C056) to Devices.config')
