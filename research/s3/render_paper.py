#!/usr/bin/env python3
"""Render the frozen English research manuscript and its numerical figures."""
from pathlib import Path
import html,re
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,Image,KeepTogether
from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
ROOT=Path(__file__).resolve().parent
WIDTH=A4[0]-108
styles=getSampleStyleSheet()
styles.add(ParagraphStyle(name='PaperTitle',fontName='Helvetica-Bold',fontSize=21,leading=25,spaceAfter=15))
styles.add(ParagraphStyle(name='PaperBody',fontName='Times-Roman',fontSize=10.4,leading=14.2,spaceAfter=8,allowWidows=0,allowOrphans=0))
styles.add(ParagraphStyle(name='PaperSmall',fontName='Times-Roman',fontSize=8.4,leading=11,spaceAfter=7))
styles.add(ParagraphStyle(name='PaperH1',fontName='Helvetica-Bold',fontSize=13,leading=16,spaceBefore=13,spaceAfter=7,keepWithNext=1))
styles.add(ParagraphStyle(name='PaperH2',fontName='Helvetica-Bold',fontSize=11,leading=14,spaceBefore=10,spaceAfter=5,keepWithNext=1))
styles.add(ParagraphStyle(name='PaperCell',fontName='Helvetica',fontSize=8,leading=10))

def inline(s):
    s=html.escape(s)
    s=re.sub(r'`([^`]+)`',lambda m:'<font name="Courier" size="8.2">'+m.group(1)+'</font>',s)
    s=re.sub(r'\*\*([^*]+)\*\*',r'<b>\1</b>',s)
    s=re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)',r'<i>\1</i>',s)
    return s

def footer(c,d):
    c.saveState();c.setFont('Helvetica',8);c.setFillGray(.35)
    c.drawString(54,A4[1]-32,'ReVerPi | S3 research manuscript | 24 September 2026')
    c.drawRightString(A4[0]-54,28,str(d.page));c.restoreState()

lines=(ROOT/'paper/manuscript.md').read_text().splitlines();story=[];i=0;references=False
while i<len(lines):
    s=lines[i].strip()
    if not s:i+=1;continue
    if s.startswith('# '):story.append(Paragraph(inline(s[2:]),styles['PaperTitle']));i+=1;continue
    if s.startswith('## '):
        references=s=='## References';story.append(Paragraph(inline(s[3:]),styles['PaperH1']));i+=1;continue
    if s.startswith('### '):story.append(Paragraph(inline(s[4:]),styles['PaperH2']));i+=1;continue
    if s.startswith('|'):
        block=[]
        while i<len(lines) and lines[i].strip().startswith('|'):
            cells=[x.strip() for x in lines[i].strip().strip('|').split('|')]
            if not all(re.fullmatch(r'[:\-]+',x) for x in cells):block.append(cells)
            i+=1
        widths=[WIDTH*.33]+[WIDTH*.67/(len(block[0])-1)]*(len(block[0])-1)
        vals=[[Paragraph(inline(x),styles['PaperCell']) for x in row] for row in block]
        t=Table(vals,colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#F0F2F4')),('LINEBELOW',(0,0),(-1,0),.65,colors.black),('LINEBELOW',(0,-1),(-1,-1),.4,colors.black),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
        story.extend([t,Spacer(1,10)]);continue
    if s.startswith('!['):
        m=re.match(r'!\[.*\]\((.*)\)',s);p=ROOT/'paper'/m.group(1)
        im=Image(str(p),width=WIDTH-8,height=(WIDTH-8)*3.2/6.5)
        group=[Spacer(1,6),im];i+=1
        while i<len(lines) and not lines[i].strip():i+=1
        if i<len(lines) and lines[i].startswith('Figure '):group.append(Paragraph(inline(lines[i]),styles['PaperSmall']));i+=1
        story.append(KeepTogether(group));continue
    block=[s];i+=1
    while i<len(lines) and lines[i].strip() and not lines[i].startswith(('#','|','![')):
        block.append(lines[i].strip());i+=1
    story.append(Paragraph(inline(' '.join(block)),styles['PaperSmall'] if references else styles['PaperBody']))
doc=SimpleDocTemplate(str(ROOT/'paper/manuscript.pdf'),pagesize=A4,rightMargin=54,leftMargin=54,topMargin=52,bottomMargin=44,title='ReVerPi: Auditing Resource-to-Answer Trade-offs',author='Research manuscript; authors pending')
doc.build(story,onFirstPage=footer,onLaterPages=footer)
print(ROOT/'paper/manuscript.pdf')
