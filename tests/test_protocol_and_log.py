import logging

from trclient.log import get_logger, redact
from trclient.protocol import apply_delta, parse_frame


def test_parse_frame():
    f = parse_frame('12 A {"a": 1}')
    assert (f.sub_id, f.code, f.body) == ("12", "A", '{"a": 1}')
    assert parse_frame("12 C").code == "C" and parse_frame("12 C").body == ""
    assert parse_frame("connected") is None
    assert parse_frame("7 X foo") is None


def test_payload_with_double_spaces_is_kept():
    f = parse_frame('3 A {"title":"a  b"}')
    assert f.body == '{"title":"a  b"}'


def test_apply_delta_replace_number():
    prev = '{"bid":{"price":13.873},"ask":{"price":13.915}}'
    # keep '{"bid":{"price":' (16), drop '13.873' (6), insert 14.001, keep the rest
    delta = "=16\t-6\t+14.001\t=" + str(len(prev) - 22)
    assert apply_delta(prev, delta) == '{"bid":{"price":14.001},"ask":{"price":13.915}}'


def test_apply_delta_url_encoded_insert():
    assert apply_delta('{"t":"x"}', "=6\t-1\t+a+b%22c\t=2") == '{"t":"a b"c"}'


def test_apply_delta_single_instruction():
    assert apply_delta("abc", "=3") == "abc"


def test_redact_cookies_and_pin():
    text = 'Cookie: tr_session=eyJabc.def; tr_refresh=xyz123 {"phoneNumber": "+49", "pin": "1234"} code=654321'
    out = redact(text)
    for secret in ("eyJabc.def", "xyz123", "1234", "654321"):
        assert secret not in out
    assert "+49" in out


def test_redact_keeps_error_codes():
    assert redact('{"errorCode":"AUTHENTICATION_ERROR"}') == '{"errorCode":"AUTHENTICATION_ERROR"}'


def test_logger_redacts(caplog):
    log = get_logger("trclient.test")
    with caplog.at_level(logging.DEBUG, logger="trclient.test"):
        log.debug("sending %s", "tr_session=SECRET")
    assert "SECRET" not in caplog.text
