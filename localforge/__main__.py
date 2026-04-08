"""Enable ``python -m localforge``."""
from localforge.cli.main import app, bootstrap_windows_scripts_path

bootstrap_windows_scripts_path()

app()
