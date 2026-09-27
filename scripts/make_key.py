"""Generate MEMORY_KEY once, on your own laptop.

    python scripts/make_key.py

Paste the printed key into Kaggle Secrets as MEMORY_KEY and ALSO save it in your
password manager. Without it, your saved memory cannot be opened by anyone,
including you.
"""
import base64
import os

print(base64.urlsafe_b64encode(os.urandom(32)).decode())
