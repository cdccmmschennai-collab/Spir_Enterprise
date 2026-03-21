"""
extraction/formats/__init__.py
All parsers in detection priority order. Add FORMAT9 here only.
"""
from __future__ import annotations
from typing import Type

from extraction.formats.base import BaseParser
from extraction.formats.format1 import Format1Parser
from extraction.formats.format2 import Format2Parser
from extraction.formats.format3 import Format3Parser
from extraction.formats.format4 import Format4Parser
from extraction.formats.format5 import Format5Parser
from extraction.formats.format6 import Format6Parser
from extraction.formats.format7 import Format7Parser
from extraction.formats.format8 import Format8Parser
from extraction.formats.adaptive import AdaptiveParser

_ALL_PARSERS: list[Type[BaseParser]] = [
    Format8Parser, Format7Parser, Format5Parser, Format6Parser,
    Format4Parser, Format1Parser, Format3Parser, Format2Parser,
    AdaptiveParser,
]

def get_all_parsers() -> list[Type[BaseParser]]:
    return list(_ALL_PARSERS)

def get_parser_for_format(format_name: str) -> Type[BaseParser] | None:
    for p in _ALL_PARSERS:
        if p.FORMAT_NAME == format_name:
            return p
    return None
