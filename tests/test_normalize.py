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
