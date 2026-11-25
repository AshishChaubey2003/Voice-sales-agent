"""Local stub server for the Chromium check (tests/js/voice-client.browser.mjs).

Runs the REAL Pipecat runner through voice/bot.py exactly as production does, so the voice
token gate, /start, the offer handler, and aiortc's SDP answer are all real. Only the bot is
a stub, so no LLM, STT, TTS, database, LangSmith, or other network call is ever made.

Safety:
  * Binds 127.0.0.1 only.
  * Generates a random signing secret per run. The secret is never printed or written;
    only a 15-minute token signed with it is printed.
  * Never reads .env: load_dotenv is neutralised and settings ignore the file.

Usage (repo root):
  .\\.venv313\\Scripts\\python.exe tests\\js\\stub_runner.py [--port 7861]
Copy the two printed lines into the environment of the browser check, then stop with Ctrl+C.
"""

import argparse
import os
import runpy
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=7861)
    args = parser.parse_args()

    # Pipecat prints emoji in its startup banner; a legacy Windows console cannot encode them.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))

    secret = secrets.token_urlsafe(48)
    os.environ.update(
        {
            "APP_ENV": "development",
            "VOICE_TOKEN_SECRET": secret,
            "GROQ_API_KEY": "stub-not-a-real-key",
            "GROQ_MODEL": "stub-model",
            "TTS_PROVIDER": "groq",
            "REDIS_ENABLED": "false",
            "LANGSMITH_TRACING": "false",
            "POSTGRES_USER": "stub",
            "POSTGRES_PASSWORD": "stub",
            "POSTGRES_DB": "stub",
            "POSTGRES_HOST": "127.0.0.1",
        }
    )
    os.environ.pop("DATABASE_URL", None)

    # Must happen BEFORE pipecat.runner.run is first imported: it calls load_dotenv(override=True).
    import dotenv

    dotenv.load_dotenv = lambda *a, **k: False
    from api.config import Settings

    Settings.model_config["env_file"] = None

    import pipecat.runner.run as pipecat_run

    async def stub_bot(runner_args):
        print("stub bot started (no audio pipeline, no external calls)", flush=True)

    class StubBotModule:
        bot = staticmethod(stub_bot)

    pipecat_run._get_bot_module = lambda: StubBotModule

    from voice.voice_auth import issue_voice_token

    token = issue_voice_token(secret.encode("utf-8"), "browser-check", 900)
    print(f"VOICE_E2E_URL=http://127.0.0.1:{args.port}")
    print(f"VOICE_E2E_TOKEN={token}", flush=True)

    sys.argv = ["bot.py", "--host", "127.0.0.1", "--port", str(args.port)]
    runpy.run_path(str(ROOT / "voice" / "bot.py"), run_name="__main__")


if __name__ == "__main__":
    main()
