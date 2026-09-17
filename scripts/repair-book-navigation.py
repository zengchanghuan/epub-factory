#!/usr/bin/env python3
"""Create a separate candidate; never publishes it or changes an order/cache."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
from app.domain.epub_targeted_repair import repair_epub

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--edits', type=Path, help='Reviewed exact text edits: [{file,old,new}]')
    parser.add_argument('--glossary', type=Path, help='Confirmed exact navigation-label mappings')
    args = parser.parse_args()
    edits = json.loads(args.edits.read_text()) if args.edits else []
    glossary = json.loads(args.glossary.read_text()) if args.glossary else {}
    print(json.dumps(repair_epub(args.source, args.output, text_edits=edits, glossary=glossary), ensure_ascii=False, indent=2))
