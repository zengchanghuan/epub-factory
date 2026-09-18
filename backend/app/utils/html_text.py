"""Map visible lexical text without rewriting IDs, URLs or XML/SVG markup."""
import re

TOKENS = re.compile(r'(<!--.*?-->|<!\[CDATA\[.*?\]\]>|<(?:"[^"]*"|\x27[^\x27]*\x27|[^\x27">])*>)', re.S)
TAG = re.compile(r'<\s*(/?)\s*([\w:-]+)')
PROTECTED = {'script', 'style', 'math', 'svg'}


def map_html_text(markup: str, transform) -> str:
    result, protected = [], []
    for index, token in enumerate(TOKENS.split(markup)):
        if index % 2:
            match = TAG.match(token)
            if match:
                closing, name = match.groups()
                name = name.lower().split(':')[-1]
                if closing and protected and name == protected[-1]: protected.pop()
                elif not closing and name in PROTECTED and not token.rstrip().endswith('/>'): protected.append(name)
            result.append(token)
        else:
            result.append(token if protected else transform(token))
    return ''.join(result)
