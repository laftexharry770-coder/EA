from xaucopier.parsing.normalize import normalize

REAL = (
    "\U0001F680 XAUUSD | BUY SIGNAL \U0001F7E2\n"
    "\U0001F4CD 4480-4485\n"
    "\U0001F534 SL: 4470\n"
    "\U0001F3AF TP1: 4490\n"
    "\U0001F3AF TP4 :4505\n"
)

def test_strips_emoji_and_keeps_lines():
    out = normalize(REAL)
    assert out.splitlines()[0] == "XAUUSD | BUY SIGNAL"
    assert "4480-4485" in out.splitlines()[1]

def test_strips_zero_width_and_nbsp():
    assert normalize("SL:​ 4470") == "SL: 4470"

def test_collapses_internal_whitespace():
    assert normalize("TP4   :4505") == "TP4 :4505"

def test_removes_thousands_separator():
    assert normalize("SL: 4,480") == "SL: 4480"

def test_converts_decimal_comma():
    assert normalize("SL: 4480,25") == "SL: 4480.25"

def test_folds_fullwidth_digits():
    assert normalize("４４８０") == "4480"

def test_drops_blank_lines():
    assert normalize("A\n\n\nB") == "A\nB"

# --- Regression tests: comma disambiguation by whole-token shape, not by
# per-comma trailing-digit count. See task-4 review finding: the original
# implementation classified a comma as "thousands" whenever exactly 3
# digits followed it, which silently corrupted 3-decimal gold prices
# (e.g. Exness XAUUSDm) by a factor of ~1000.

def test_three_decimal_comma_on_gold_feed_is_decimal_not_thousands():
    # The reported defect: normalize("4480,255") used to yield "4480255".
    assert normalize("SL: 4480,255") == "SL: 4480.255"

def test_multiple_grouped_thousands_separators_collapse():
    assert normalize("1,234,567") == "1234567"

def test_malformed_grouping_is_left_unchanged():
    # "4,4805" is neither a properly grouped thousands run (the group
    # after the comma is 4 digits, not 3) nor a plausible price decimal
    # (this bot's domain only sees 1-3 fractional digits). Left alone
    # rather than silently reinterpreted as the valid-looking "4.4805".
    assert normalize("4,4805") == "4,4805"

def test_comma_in_prose_is_untouched():
    assert normalize("hello, world") == "hello, world"

def test_trailing_comma_after_digit_is_untouched():
    assert normalize("SL: 4470,") == "SL: 4470,"

# --- Fix round 2: a numeric run with two or more commas where the trailing
# group is not exactly three digits (thousands separator AND decimal comma
# in the same number). See task-4 fix-round-2 brief: the two-independent-
# pass design left these commas unhandled by _THOUSANDS_GROUP (the whole
# run isn't well-formed grouping) and then converted *every* remaining
# inter-digit comma via _DECIMAL_COMMA, producing two decimal points
# (e.g. "4,480,25" -> "4.480.25" instead of "4480.25").

def test_grouped_thousands_with_two_digit_decimal_tail():
    assert normalize("4,480,25") == "4480.25"

def test_grouped_thousands_with_two_digit_decimal_tail_alt():
    assert normalize("1,234,56") == "1234.56"

def test_grouped_thousands_with_one_digit_decimal_tail():
    assert normalize("12,345,6") == "12345.6"
