#!/usr/bin/env python3
"""Load only this repository's .env, never a parent one."""
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / '.env', override=False)
from eval.run_batch import main
if __name__ == '__main__':
    raise SystemExit(main())
