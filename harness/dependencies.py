"""Initialize application-private Python dependencies in source checkouts."""
import site
import sys
from pathlib import Path


def bootstrap():
    vendor = Path(__file__).resolve().parent.parent / 'vendor/python'
    if vendor.is_dir() and str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
        site.addsitedir(str(vendor))


bootstrap()
