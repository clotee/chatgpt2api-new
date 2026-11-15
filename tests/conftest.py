import os

# services.config validates auth-key at import time; force the test key so
# outer env (e.g. CI's ci-smoke) cannot diverge from the hardcoded test key.
os.environ["CHATGPT2API_AUTH_KEY"] = "test-auth-key"
