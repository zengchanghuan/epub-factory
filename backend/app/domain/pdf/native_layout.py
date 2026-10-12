"""Conservative single-column text-layer blocks, using PDFium coordinates/fonts.

Never repairs spelling/hyphens. Short or ambiguous lines stay separate; scans,
rotated text and multi-column pages are already routed to OCR by local.py.
"""
import ctypes
import math
import re
import statistics
from .content import _join_prose


def extract_blocks(tp):
    import pypdfium2 as pdfium
    raw=pdfium.raw
    lines=[];chars=[];indices=[]
    def flush():
        if not chars or not ''.join(chars).strip(): return
        nonwhite=[i for i,c in zip(indices,chars) if not c.isspace()]
        if not nonwhite:return
        boxes=[tp.get_charbox(i) for i in nonwhite]
        # Sampling fonts avoids a ctypes allocation for every source character.
        sample=nonwhite[::max(1,len(nonwhite)//8)]
        mono=[];sizes=[]
        for i in sample:
            buffer=ctypes.create_string_buffer(256);flags=ctypes.c_int()
            raw.FPDFText_GetFontInfo(tp,i,buffer,len(buffer),ctypes.byref(flags))
            name=buffer.value.decode('utf8',errors='replace')
            mono.append(bool(flags.value & 1) or bool(re.search('courier|mono|(?:cm|sf|lm)tt|typewriter|consolas',name,re.I)))
            matrix = raw.FS_MATRIX()
            scale = math.hypot(matrix.c, matrix.d) if raw.FPDFText_GetMatrix(tp, i, ctypes.byref(matrix)) else 1
            sizes.append(raw.FPDFText_GetFontSize(tp,i) * scale)
        lines.append({'text':''.join(chars).rstrip(), 'x':min(b[0] for b in boxes),
                      'right':max(b[2] for b in boxes),'y':statistics.median((b[1]+b[3])/2 for b in boxes),
                      'size':statistics.median(sizes),'mono':all(mono)})
    for i in range(tp.count_chars()):
        value=raw.FPDFText_GetUnicode(tp,i)
        char=chr(value) if value else ''
        if char in {'\r','\n'}:
            flush();chars=[];indices=[]
        else:chars.append(char);indices.append(i)
    flush()
    if not lines:return []
    # Body size weighted by character count, so several small headings cannot
    # dominate a page with long paragraphs.
    sizes=[line['size'] for line in lines for _ in range(len(line['text'].strip()))]
    body_size=statistics.median(sizes)
    cjk_gaps = [a['y']-b['y'] for a,b in zip(lines,lines[1:])
                if re.search(r'[\u3400-\u9fff]', a['text'])
                and abs(a['size']-body_size)<.5 and abs(b['size']-body_size)<.5
                and body_size*.8<a['y']-b['y']<=body_size*2.2]
    cjk_leading = statistics.median(cjk_gaps) if len(cjk_gaps)>=2 else body_size*1.5
    prose=[line for line in lines if not line['mono'] and line['size']<=body_size*1.15]
    right=max((line['right'] for line in prose),default=0)
    left=min((line['x'] for line in prose),default=0)
    # Fixed-width narrative is still translatable. Font evidence alone must not
    # turn a typewritten novel into protected code. Require syntax in a nearby
    # contiguous fixed-width run; that run may also include interpreter output.
    code_lines=set();run=[]
    syntax=re.compile(r"^\s*(?:>>>|\.\.\.|<class\s|#include|//|(?:def|class|import|from|for|while|if|elif|else|return|print|raise|try|except)\b.*[:(]|[A-Za-z_]\w*\s*=(?!=))")
    def finish_run():
        if any(syntax.search(lines[i]['text']) for i in run):code_lines.update(run)
        run.clear()
    for i,line in enumerate(lines):
        if not line['mono']:
            finish_run();continue
        if run and not 0<lines[run[-1]]['y']-line['y']<=line['size']*1.7:finish_run()
        run.append(i)
    finish_run()
    output=[];previous=None
    for index,line in enumerate(lines):
        text=line['text'];size=line['size']
        kind='code' if index in code_lines else 'heading' if size>body_size*1.15 else 'text'
        if kind=='text' and re.match(r'^\s*(?:[•●▪*-]|\d+[.)])\s+',text):kind='list'
        block={'type':kind,'text':text,'source_text':text,'source_line_indices':[index]}
        merge=False
        if previous and output[-1]['type']==kind and kind in {'text','code'}:
            gap=previous['y']-line['y']
            aligned=abs(previous['x']-line['x'])<=max(2,size*.4)
            close=0<gap<=size*1.65 and abs(previous['size']-size)<.5
            # A wrap requires the preceding line to reach the body right edge.
            # Do not join short poem/list lines or infer missing hyphens.
            wrap=previous['right']>=right-max(size*2.15,(right-left)*.06)
            cjk = bool(re.search(r'[\u3400-\u9fff]', previous['text']))
            if cjk:
                close=0<gap<=min(size*2.25,cjk_leading*1.12) and abs(previous['size']-size)<.5
            # Chinese first lines may start two ems in. A following indented
            # line starts a new paragraph, even if the preceding line is full.
            continuation = abs(line['x']-left)<=max(2,size*.4)
            first_indent = 0<=previous['x']-left<=size*2.5
            aligned = (continuation and first_indent) if cjk else aligned
            merge=close and (kind=='code' or aligned and wrap and len(previous['text'])>=(12 if cjk else 45))
        if merge:
            output[-1]['text']=(output[-1]['text']+'\n'+text if kind=='code' else _join_prose(output[-1]['text'],text))
            output[-1]['source_text']+='\n'+text
            output[-1]['source_line_indices'].append(index)
        else:output.append(block)
        previous=line
    return output
