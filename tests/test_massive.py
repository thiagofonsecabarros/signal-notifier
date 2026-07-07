from unittest.mock import Mock

import pytest

from stock_notifier.providers.base import MarketDataNotAvailableError
from stock_notifier.providers.massive import MassiveClient


def response(status_code, payload, text=""):
    result = Mock()
    result.status_code = status_code
    result.json.return_value = payload
    result.text = text
    result.headers = {}
    return result


def test_before_end_of_day_403_is_typed_as_temporarily_unavailable():
    session = Mock()
    session.get.return_value = response(
        403,
        {
            "status": "NOT_AUTHORIZED",
            "message": "Attempted to request today's data before end of day.",
        },
    )
    client = MassiveClient("key", requests_per_minute=100000, session=session)

    with pytest.raises(MarketDataNotAvailableError):
        client._get("/test")


def test_other_403_remains_a_hard_failure():
    session = Mock()
    session.get.return_value = response(
        403, {"status": "NOT_AUTHORIZED", "message": "API key is not authorized"}, "forbidden"
    )
    client = MassiveClient("key", requests_per_minute=100000, session=session)

    with pytest.raises(RuntimeError, match="HTTP 403"):
        client._get("/test")
