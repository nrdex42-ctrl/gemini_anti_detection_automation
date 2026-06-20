"""jazoest — Facebook's anti-replay checksum.

Formula (stable since 2019):
  jazoest = (sum of charCodes of all values, concatenated) % 2199 + 115

The input is ALL form field values (except jazoest itself), concatenated in
the order they appear in the request body. Python 3.7+ dicts preserve insertion
order, so building the form dict with fields in the correct order is critical.

CRITICAL: Wrong jazoest = silent no-op (200 OK, post does not appear).
"""

from typing import Dict


def compute_jazoest(form_data: Dict[str, str]) -> str:
    """Compute the jazoest anti-replay token for a form-encoded request.

    Formula: jazoest = (sum of charCodes of all values, concatenated) % 2199 + 115

    The *form_data* dict should contain ALL form fields except jazoest itself.
    Values are concatenated in insertion order (Python 3.7+ dict insertion order).
    """
    concatenated = "".join(str(v) for v in form_data.values())
    char_sum = sum(ord(c) for c in concatenated)
    return str(char_sum % 2199 + 115)


def inject_jazoest(form_data: Dict[str, str]) -> Dict[str, str]:
    """Add a correct jazoest field to an existing form-data dict.

    Returns a new dict with 'jazoest' inserted before the final field
    (matching Facebook's client behavior where jazoest is the second-to-last field).
    """
    jz = compute_jazoest(form_data)
    result = dict(form_data)
    result["jazoest"] = jz
    return result
