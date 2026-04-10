#!/bin/bash
# Yerel geliştirme için — backend'i başlatır
cd "$(dirname "$0")"
pip install -r requirements.txt -q
uvicorn main:app --reload --port 8000
