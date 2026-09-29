from datetime import date

from src.execution.position_reevaluator import parse_ticker_date


def test_ticker_date_is_yy_mmm_dd():
    assert parse_ticker_date("KXHIGHNY-26FEB12-T32") == date(2026, 2, 12)
    assert parse_ticker_date("KXLOWTNYC-26SEP05-B14.5") == date(2026, 9, 5)


def test_ticker_date_invalid():
    assert parse_ticker_date("KXHIGHNY") is None
    assert parse_ticker_date("KXHIGHNY-26XYZ12-T32") is None
    assert parse_ticker_date("KXHIGHNY-26FEB30-T32") is None
