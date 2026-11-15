import os
import sys
from pathlib import Path


def user_data_root():
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'UltraRoute'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'ultraroute'


def default_data_dir():
    if os.getenv('ULTRAROUTE_DATA_DIR'):
        return Path(os.environ['ULTRAROUTE_DATA_DIR'])
    checkout = Path(__file__).resolve().parent.parent
    if (checkout / 'pyproject.toml').is_file() and (checkout / 'ultraroute').is_dir():
        return checkout / '.data'
    return user_data_root() / 'state'
