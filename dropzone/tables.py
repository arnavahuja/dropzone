"""Static lookup tables keyed by ISO country code.

These are the only place a country code is consumed. The code itself never
leaves the server: tools read it from session state and return a derived fact
(which side traffic drives on, a greeting in the local script) instead.
"""

from __future__ import annotations

# Countries where traffic keeps left. Everywhere else is assumed right-hand.
DRIVES_LEFT = {
    "AG", "AU", "BB", "BD", "BN", "BS", "BW", "BT", "CY", "DM", "FJ", "GB",
    "GD", "GY", "HK", "ID", "IE", "IN", "JM", "JP", "KE", "KI", "KN", "LC",
    "LK", "LS", "MO", "MT", "MU", "MV", "MW", "MY", "MZ", "NA", "NP", "NR",
    "NZ", "PG", "PK", "SB", "SC", "SG", "SR", "SZ", "TH", "TL", "TO", "TT",
    "TV", "TZ", "UG", "VC", "WS", "ZA", "ZM", "ZW",
}

# Countries posting speed limits in miles per hour.
USES_MPH = {"US", "GB", "LR", "MM", "AG", "BS", "BZ", "DM", "GD", "GY", "KN",
            "LC", "VC", "WS", "VG", "KY", "BM", "PR", "VI", "GU", "AS", "MP"}

# "Hello" in the dominant local language, in its native script. The language is
# deliberately not named: identifying the script and the word is the puzzle.
GREETINGS = {
    "AE": "مرحبا", "AM": "բարև", "AR": "hola", "AT": "grüß gott",
    "AU": "hello", "BD": "নমস্কার", "BO": "hola", "BR": "olá",
    "BT": "ཁམས་བཟང་", "BW": "dumela", "CA": "hello", "CH": "grüezi",
    "CL": "hola", "CN": "你好", "CO": "hola", "CU": "hola", "CZ": "dobrý den",
    "DE": "guten tag", "DK": "goddag", "DZ": "مرحبا", "EC": "hola",
    "EE": "tere", "EG": "السلام عليكم", "ES": "hola", "ET": "ሰላም",
    "FI": "hei", "FJ": "bula", "FR": "bonjour", "GB": "hello", "GE": "გამარჯობა",
    "GH": "hello", "GR": "γεια σας", "GT": "hola", "HK": "你好", "HR": "dobar dan",
    "HU": "jó napot", "ID": "selamat siang", "IE": "hello", "IL": "שלום",
    "IN": "नमस्ते", "IQ": "السلام عليكم", "IR": "سلام", "IS": "góðan dag",
    "IT": "buongiorno", "JO": "السلام عليكم", "JP": "こんにちは", "KE": "jambo",
    "KH": "សួស្តី", "KR": "안녕하세요", "LA": "ສະບາຍດີ", "LB": "مرحبا",
    "LK": "ආයුබෝවන්", "MA": "السلام عليكم", "MG": "manao ahoana", "MM": "မင်္ဂလာပါ",
    "MN": "сайн байна уу", "MX": "hola", "MY": "selamat tengah hari",
    "MZ": "olá", "NA": "hallo", "NG": "hello", "NL": "goedendag", "NO": "god dag",
    "NP": "नमस्ते", "NZ": "kia ora", "PE": "hola", "PH": "kumusta",
    "PK": "السلام علیکم", "PL": "dzień dobry", "PT": "olá", "PY": "hola",
    "RO": "bună ziua", "RS": "добар дан", "RU": "здравствуйте", "SA": "السلام عليكم",
    "SE": "hej", "SG": "hello", "SI": "dober dan", "SK": "dobrý deň",
    "SN": "salaam aleekum", "SR": "fa waka", "SY": "مرحبا", "TH": "สวัสดี",
    "TN": "مرحبا", "TR": "merhaba", "TW": "你好", "TZ": "jambo", "UA": "добрий день",
    "UG": "oli otya", "US": "hello", "UY": "hola", "UZ": "salom", "VN": "xin chào",
    "ZA": "hallo", "ZM": "muli bwanji", "ZW": "mhoro",
}


def drives_on(country_code: str | None) -> str:
    """'left' or 'right' for a country code; 'unknown' if we have no code."""
    if not country_code:
        return "unknown"
    return "left" if country_code.upper() in DRIVES_LEFT else "right"


def speed_unit(country_code: str | None) -> str:
    """The unit speed limit signs are posted in."""
    if not country_code:
        return "unknown"
    return "mph" if country_code.upper() in USES_MPH else "km/h"


def greeting_for(country_code: str | None) -> str | None:
    """The local word for hello, or None when we have nothing for the country."""
    if not country_code:
        return None
    return GREETINGS.get(country_code.upper())
