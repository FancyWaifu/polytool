#!/usr/bin/env python3
"""
Clockwork Client — Connect to Poly Lens Control Service (LensService).

Connects via TCP to the LensServiceApi to discover and modify headset
settings using Poly's own protocol translation layer.

Works alongside Poly Lens without modifying it. Also works with our
own LensServer (lensserver.py) as a drop-in replacement.

Protocol (reverse-engineered from Poly Studio app.asar):
  Port: ~/Library/Application Support/Poly/Lens Control Service/SocketPortNumber
  Transport: TCP with SOH (\\x01) delimited JSON
  Messages: {"type": "MessageType", "apiVersion": "1.0.0", ...}
  Registration: {"type": "RegisterClient", "useEncryption": false}

Usage:
  python3 clockwork_client.py discover       # Find devices + all settings
  python3 clockwork_client.py get <setting>  # Read a setting
  python3 clockwork_client.py set <setting> <value>  # Write a setting
  python3 clockwork_client.py dump           # Export all settings as JSON
  python3 clockwork_client.py monitor        # Watch device events
"""

# This module now wraps lensapi.py which has the correct protocol implementation.
# Kept for backwards compatibility.

from lensapi import main

if __name__ == "__main__":
    main()
