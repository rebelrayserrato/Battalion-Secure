from pathlib import Path
import os
import subprocess
import sys


if __name__ == "__main__":
    app = Path(__file__).parent / "app" / "main.py"
    env = os.environ.copy()
    parent = str(app.parents[2])
    env["PYTHONPATH"] = parent + os.pathsep + env.get("PYTHONPATH", "")

    # RAYAAAA-210: matter-API sidecar. Runs alongside Streamlit in this container,
    # sharing the same sqlite store, so the owner portal can create a matter over
    # the internal auth-gated path (nginx /admin/review-engine/api/ -> :8600).
    # Bound to 0.0.0.0:8600 INSIDE the container only (compose uses `expose`, never
    # `ports`); nginx is the sole ingress.
    api_port = os.environ.get("MATTER_API_PORT", "8600")
    api_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "review_engine.api.matter_api:app",
            "--host",
            "0.0.0.0",
            "--port",
            api_port,
        ],
        env=env,
    )

    try:
        raise SystemExit(
            subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)], env=env)
        )
    finally:
        api_proc.terminate()
