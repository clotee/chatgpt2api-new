import os

# services.config validates auth-key at import time; provide a test default.
os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth-key")
