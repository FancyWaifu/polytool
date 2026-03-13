#!/usr/bin/env python3
"""Add Blackwire 3220 to Poly Lens Devices.config so it stops being classified as bricked."""
import json

path = '/Applications/Poly Studio.app/Contents/Helpers/LegacyHostApp.app/Contents/Resources/Devices.config'
with open(path) as f:
    d = json.load(f)

d['devices'].append({
    'ProductID': 'C056',
    'usagePage': 'FFA0',
    'Name': 'Blackwire 3220',
    'DeviceEventHandler': '',
    'HostCommandHandler': 'R3HostCommand',
    'DeviceListenerHandler': '',
    'HostCommandDelay': ''
})

with open(path, 'w') as f:
    json.dump(d, f, indent=2)

print('Added Blackwire 3220 (C056) to Devices.config')
