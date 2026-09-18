"""Map visible lexical text without rewriting IDs, URLs or XML/SVG markup."""
import re
from html import escape

TOKENS = re.compile(r'(<!--.*?-->|<!\[CDATA\[.*?\]\]>|<(?:"[^"]*"|\x27[^\x27]*\x27|[^\x27">])*>)', re.S)
TAG = re.compile(r'<\s*(/?)\s*([\w:-]+)')
PROTECTED = {'script', 'style', 'math', 'svg', 'pre', 'code', 'kbd', 'samp', 'tt'}
ATTRIBUTE = re.compile(r'''([^\s"'<>/=]+)(\s*=\s*)("[^"]*"|'[^']*'|[^\s>]+)''', re.S)


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


def map_html_styles(markup: str, transform) -> str:
    """Transform real CSS only, preserving literal code and technical markup.

    Tokenization retains original tag spelling, attribute casing and whitespace;
    CSS examples in prose/pre/code are never treated as a stylesheet.
    """
    result, protected = [], []
    in_style = False
    def map_attribute(match):
        # Consume every complete attribute, including quoted values, so text
        # such as title="style='...'" can never masquerade as a style attribute.
        name, equals, value = match.groups()
        if name.lower() != 'style':
            return match[0]
        if value[0] in {'"', "'"}:
            return name + equals + value[0] + transform(value[1:-1]) + value[0]
        rewritten = transform(value)
        if rewritten == value:
            return match[0]
        # Legacy HTML may have an unquoted style. Add quotes if rewriting adds
        # spaces instead of emitting an invalid unquoted attribute value.
        return name + equals + '"' + escape(rewritten, quote=True) + '"'
    for index, token in enumerate(TOKENS.split(markup)):
        if index % 2:
            match = TAG.match(token)
            if match:
                closing, name = match.groups()
                name = name.lower().split(':')[-1]
                if closing and protected and name == protected[-1]:
                    protected.pop()
                elif not closing and name in PROTECTED - {'style'} and not token.rstrip().endswith('/>'):
                    protected.append(name)
                elif not protected:
                    if name == 'style':
                        in_style = not closing and not token.rstrip().endswith('/>')
                    if not closing:
                        token = ATTRIBUTE.sub(map_attribute, token)
            elif in_style and not protected and token.startswith('<![CDATA['):
                token = '<![CDATA[' + transform(token[9:-3]) + ']]>'
            result.append(token)
        else:
            result.append(transform(token) if in_style and not protected else token)
    return ''.join(result)
