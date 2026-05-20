"""
Format adapters: convert non-EPUB source files to a minimal EPUB
so they can be passed into ExtremeCompiler unchanged.

Each adapter exposes a single function:
    adapter_to_html(path: Path) -> tuple[str, dict]

The returned tuple is (html_body_string, metadata_dict) where
metadata_dict may contain: title, author, language, identifier.
"""
