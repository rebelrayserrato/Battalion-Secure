from pathlib import Path
import os
import subprocess
import sys


if __name__ == "__main__":
    app = Path(__file__).parent / "app" / "main.py"
    env = os.environ.copy()
    parent = str(app.parents[2])
    env["PYTHONPATH"] = parent + os.pathsep + env.get("PYTHONPATH", "")
    raise SystemExit(
        subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)], env=env)
    )
