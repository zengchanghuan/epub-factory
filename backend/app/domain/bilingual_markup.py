"""Shared content-model choice for both bilingual writeback paths."""


def bilingual_wrapper_tag(parent_name: str) -> str:
    # These source blocks accept flow content (including tables/lists). A
    # span would make a valid source table invalid after bilingual injection.
    # Paragraphs/headings must continue using phrasing-only spans.
    return 'div' if parent_name in {'div', 'li', 'blockquote'} else 'span'
