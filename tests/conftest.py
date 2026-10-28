"""Suite-wide safety: tests must never send traces to a real LangSmith workspace.

With the optional SDK installed and LANGSMITH_TRACING plus a key in the developer's
.env, any test that reaches an instrumented function (for example a chat-route test
saving a lead) would otherwise create real runs in their project. A process
environment variable takes precedence over .env, so this is enough to keep every
test local. tests/test_tracing.py builds its own settings objects and fakes the SDK.
"""

import os

os.environ["LANGSMITH_TRACING"] = "false"
