"""Where test cases come from: live Jira/Xray, or raw-Xray-shaped JSON files on disk."""

from ..core.config import Config
from ..core.models import ManualTestCase
from .local import load_local_test_case
from .xray import XrayClient

__all__ = ["XrayClient", "load_local_test_case", "load_test_case"]


def load_test_case(config: Config, issue_key: str) -> ManualTestCase:
    """Fetch one test case from the configured source.

    ``TESTCASE_SOURCE=local`` reads a raw-Xray-shaped JSON file from ``LOCAL_TESTCASE_DIR``
    (no Jira needed); the default ``xray`` source fetches it live from Jira/Xray. Both yield
    the same ``ManualTestCase``, so everything downstream is identical.
    """
    if config.testcase_source == "local":
        return load_local_test_case(config, issue_key)
    return XrayClient(config).fetch(issue_key)
