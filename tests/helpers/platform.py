import sys

import pytest

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX mode bits / symlinks / the bash wrapper; the Windows ACL checks are in tests/client/test_windows_acl.py",
)
