"""
The JSON encoding of non-finite floats in archive records.

Archive rows legitimately carry infinities and NaN: an inelastic vertex's price is infinite, a degenerate curve can
yield an infinite or undefined cost. JSON has no token for them (Python's ``Infinity``/``NaN`` are extensions other
parsers refuse), and a transport that insists on standard JSON (``allow_nan=False``) cannot carry them as numbers. A
record that travels (the VolttronDataManager publishes to the historian) is therefore passed through
``encode_non_finite`` first, which replaces every non-finite float with the object

    {"$float": "inf"} | {"$float": "-inf"} | {"$float": "nan"}

and the receiving LocalDataManager decodes the object back into the float when it normalises a row
(``decode_non_finite``). The object form is chosen over strings ("Infinity", "NaN") because it is valid JSON for every
parser, cannot be mistaken for a legitimate text value of a String column, and names what it is: a key beginning with
``$`` is not a column name. Finite floats, and every other value, pass through unchanged; the transport's own encoder
still handles datetimes, UUIDs and enumerations.

What the database does with the decoded value is the database's: SQLite stores infinities as REAL and reads them back,
but stores NaN as NULL (it has no NaN), so a NaN cost reads back as None from SQLite and as NaN from PostgreSQL. A
LocalDataManager writing straight to SQLite (no JSON on the way) has always behaved so.
"""
import math
from collections.abc import Mapping, Sequence

NON_FINITE_KEY = '$float'
TOKENS = {'inf': math.inf, '-inf': -math.inf, 'nan': math.nan}


def encode_non_finite(value):
    """`value` with every non-finite float (at any depth of mappings and lists) replaced by its {"$float": token}
    object. Mappings and lists are copied; everything else is returned as is."""
    if isinstance(value, float) and not math.isfinite(value):
        return {NON_FINITE_KEY: 'nan' if math.isnan(value) else 'inf' if value > 0 else '-inf'}
    if isinstance(value, Mapping):
        return {key: encode_non_finite(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [encode_non_finite(item) for item in value]
    return value


def is_non_finite_token(value) -> bool:
    return isinstance(value, Mapping) and NON_FINITE_KEY in value


def decode_non_finite(value):
    """The float a {"$float": token} object stands for; any other value unchanged. An object with the key and an
    unknown token, or other keys beside it, is a ValueError."""
    if not is_non_finite_token(value):
        return value
    if set(value) != {NON_FINITE_KEY} or value[NON_FINITE_KEY] not in TOKENS:
        raise ValueError(f'{value!r} is not a non-finite float token; the tokens are '
                         f'{{"{NON_FINITE_KEY}": "inf" | "-inf" | "nan"}}.')
    return TOKENS[value[NON_FINITE_KEY]]
