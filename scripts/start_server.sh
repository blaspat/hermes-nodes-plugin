#!/bin/bash
source /home/patrick/.hermes/.env
exec /home/patrick/.hermes/hermes-agent/venv/bin/python /home/patrick/hermes-nodes-plugin/scripts/run_server.py
