# CallRail → CTM Migration Packet Generator

Generates a Gmail-safe HTML migration packet per CallRail account accessible by a CallRail API token.
Posts HTML to a Make.com webhook which emails via Gmail.

## Requirements
- Python 3.10+
- requests

## Install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

## Environment
- CALLRAIL_API_KEY
- OPENAI_API_KEY

## Run
python callrail_migration_script.py --log-level INFO
python callrail_migration_script.py --log-level DEBUG
