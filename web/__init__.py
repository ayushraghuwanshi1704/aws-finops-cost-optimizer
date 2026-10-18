"""
CloudLedger-style dashboard presentation layer.

Deliberately dependency-free: the dashboard is served with the Python standard
library (http.server), so a demo run needs nothing beyond this repo's existing
boto3 install and keeps working with no network at all. Analysis logic stays in
scripts/ - web/ only assembles and renders what those modules already compute.

Importing this package puts scripts/ on sys.path (the same way tests/conftest.py
does) so `python -m web.app` works from a clean checkout.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
