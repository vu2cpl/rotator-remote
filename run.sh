#!/bin/bash
# Start the rotator-remote server in the foreground.
cd "$(dirname "$0")"
exec venv/bin/python server.py "$@"
