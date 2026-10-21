"""Strict parsers for openFPGALoader output (--detect, --read-dna, --read-xadc).

Every parser raises ValueError on anything it cannot read, so a failed or garbled measurement
stops the caller instead of being treated as "no data".
"""
import json
import math
import re


def parse_idcode(text):
    m = re.search(r"idcode\s+(0x[0-9a-fA-F]+)", text)
    if not m:
        raise ValueError("no IDCODE in output")
    return m.group(1).lower()


def parse_dna(text):
    m = re.search(r'"dna"\s*:\s*"(0x[0-9a-fA-F]{1,16})"', text)
    if not m:
        raise ValueError("no DNA in output")
    return "0x%016x" % int(m.group(1), 16)


def parse_xadc(text):
    """openFPGALoader prints banner lines, then an object with a trailing comma."""
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise ValueError("no XADC object in output")
    try:
        obj = json.loads(re.sub(r",\s*}", "}", text[a:b + 1]))
    except json.JSONDecodeError as e:
        raise ValueError("XADC object is not valid JSON: %s" % e)
    t = obj.get("temp")
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t) or not (-60.0 < t < 200.0):
        raise ValueError("XADC temperature missing or not a plausible finite number: %r" % (t,))
    return obj
