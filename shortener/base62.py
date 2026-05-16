"""
Base62 encoder.

Alphabet: 0-9, a-z, A-Z (62 chars). URL-safe, compact (64-bit id fits in
<= 11 chars), and case-sensitive (doubles the alphabet vs base36).
"""

ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
BASE = len(ALPHABET)  # 62


def encode(n: int) -> str:
    if n < 0:
        raise ValueError('base62 encoding is only defined for non-negative integers')
    if n == 0:
        return ALPHABET[0]
    chars = []
    while n > 0:
        n, rem = divmod(n, BASE)
        chars.append(ALPHABET[rem])
    return ''.join(reversed(chars))
