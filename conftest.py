import os
import tempfile
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/db_test.sqlite3")
os.environ.setdefault("DJANGO_SECRET_KEY", "test")
os.environ.setdefault("DJANGO_DEBUG", "True")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault(
    "UAV_STORAGE_ROOT",
    str(Path(tempfile.gettempdir()) / "mission-planner-pytest-storage"),
)
