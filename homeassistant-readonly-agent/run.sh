#!/bin/sh
set -eu

exec /opt/ha-agent-venv/bin/python -m app.main
