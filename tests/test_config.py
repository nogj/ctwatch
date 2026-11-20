import pytest

from ctwatch.config import DEFAULT_POLL_SECONDS, positive_int
from infra.render import render


def test_default_polling_matches_infrastructure():
    assert DEFAULT_POLL_SECONDS == 15
    assert render()["service.json"]["Parameters"]["PollSeconds"]["Default"] == str(
        DEFAULT_POLL_SECONDS
    )


@pytest.mark.parametrize("value", ["0", "-1", "nan", "1.5", ""])
def test_invalid_polling(value):
    with pytest.raises(ValueError):
        positive_int(value)


def test_custom_polling():
    assert positive_int("30") == 30
