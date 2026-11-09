"""Clearly synthetic reports for private staging and evidence verification."""
from io import BytesIO
from pathlib import Path

def report(serial='005545-1',grid=True,missing=False):
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    font=next(p for p in [Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),Path('C:/Windows/Fonts/arial.ttf')] if p.exists())
    if 'SyntheticFont' not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont('SyntheticFont',str(font)))
    output=BytesIO();canvas=Canvas(output,pagesize=(595,842),invariant=1)
    canvas.setFont('SyntheticFont',12)
    for y,line in [(800,'SYNTHETIC TEST DOCUMENT - NOT CLIENT DATA'),(770,'ВКТ-7 Заводской номер: '+serial),
                   (745,'(01.09.2026 0:00:00 - 03.09.2026 0:00:00)')]:canvas.drawString(40,y,line)
    rows=[['Дата','t1','Q'],['01.09.2026','10','20'],['02.09.2026','11','21'],['03.09.2026','12','22']]
    if missing:rows[-1][-1]='-'
    for i,row in enumerate(rows):
        y=700-30*i
        if grid:
            for x,value in zip([45,200,300],row):canvas.drawString(x,y,value)
        else:canvas.drawString(45,y,' '.join(row))
    if grid:
        for x in (40,190,290,390):canvas.line(x,680-30*3,x,725)
        for y in (725,695,665,635,605):canvas.line(40,y,390,y)
    canvas.save();return output.getvalue()

def extraction(serial='005545-2'):
    return {'serial_candidates':[serial],'instrument_model':'ВКТ-7','billing_period':['2026-09-01','2026-09-03'],
        'series':[{'headers':['Дата','t1','Q'],'required_columns':[1,2],
            'rows':[{'cells':row,'evidence':{'page':1,'quote':' '.join(row)}} for row in [
                ['01.09.2026','10','20'],['02.09.2026','11','21'],['03.09.2026','12','22']]],
            'evidence':{'page':1,'quote':'Дата t1 Q'}}],
        'evidence':[{'page':1,'quote':'ВКТ-7 Заводской номер: '+serial}],
        'ambiguities':[],'parser_engine':'cloudflare-ai','prompt_version':'extract-evidence/1'}
