"""Shared FTS5 helpers: character bigrams and honest query expressions.

Chinese has no word delimiters, so both the index and the query are character
bigrams (plus latin words). FTS5 does the matching and ranking; this module
never turns a lexical score into a semantic one.
"""
from __future__ import annotations

import hashlib
import re

HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]+')
WORD = re.compile(r'[a-z0-9_]{2,}')


def terms(text):
    """Unique index/query terms in appearance order: Han bigrams plus latin words."""
    value = str(text or '').casefold()
    found = []
    for run in HAN.findall(value):
        if len(run) == 1:
            found.append(run)
        else:
            found.extend(run[index:index + 2] for index in range(len(run) - 1))
    found.extend(WORD.findall(value))
    return list(dict.fromkeys(term for term in found if term))


def bigrams(text):
    return ' '.join(terms(text))


def digest(text):
    return hashlib.sha256(str(text or '').encode('utf-8')).hexdigest()


def match(text, mode='OR'):
    """A quoted FTS5 MATCH expression over every term, or '' when there is none.

    AND first keeps a short phrase precise; the same terms ORed let a long
    question match on its head and its tail without dropping either end.
    """
    parts = ['"' + term.replace('"', '""') + '"' for term in terms(text)]
    if not parts:
        return ''
    joiner = ' AND ' if str(mode or 'OR').upper() == 'AND' else ' OR '
    return joiner.join(parts)


def expressions(text):
    """(AND expression, OR expression) for one query; either may be empty."""
    return match(text, 'AND'), match(text, 'OR')
