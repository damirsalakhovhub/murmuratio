#!/bin/bash
# Open the project's durable user LaunchAgent preview.
set -e
label=dev.local-sites.murmuratio
plist="$HOME/Library/LaunchAgents/$label.plist"
if ! launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
  launchctl bootstrap "gui/$(id -u)" "$plist"
fi
open http://agent-inspector.localhost:1355/
