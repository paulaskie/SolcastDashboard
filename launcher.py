"""PyInstaller entry point for the Solcast Streamlit dashboard."""
import runpy
import os
import sys
from pathlib import Path


def main():
    resource_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    if sys.argv[1:2] == ["--openmeteo"]:
        sys.argv = ["openmeteo.py", *sys.argv[2:]]
        runpy.run_path(str(resource_dir / "openmeteo.py"), run_name="__main__")
        return

    app_path = resource_dir / "app.py"
    # PyInstaller extracts Streamlit outside site-packages, which can make
    # Streamlit mistake the frozen app for a development install and omit the
    # production frontend route.
    os.environ["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] = "false"
    sys.argv = ["streamlit", "run", str(app_path), "--server.headless=true"]
    from streamlit.web.cli import main as streamlit_main
    streamlit_main()


if __name__ == "__main__":
    main()
