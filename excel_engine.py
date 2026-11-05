"""Template-preserving XLSX importer/exporter.

Updates only the original content cells and adds drawing/image parts, rather than
rebuilding an Excel workbook. All pre-existing workbook structure and styles remain.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from io import BytesIO
import os
import posixpath
import re
import zipfile

from lxml import etree as ET
from PIL import Image, ImageOps

S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
XDR = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'

CELL_RE = re.compile(r'^([A-Z]+)(\d+)$')
RANGE_RE = re.compile(r'^([A-Z]+)(\d+):([A-Z]+)(\d+)$')
NS = {'m': S}


def local(tag):
    return ET.QName(tag).localname


def col_num(label: str) -> int:
    out = 0
    for ch in label:
        out = out * 26 + ord(ch) - 64
    return out


def col_label(n: int) -> str:
    result = ''
    while n:
        n, rem = divmod(n-1, 26)
        result = chr(65+rem) + result
    return result


def address_parts(addr: str):
    m = CELL_RE.match(addr)
    if m is None:
        raise ValueError('Invalid cell address: '+addr)
    return col_num(m.group(1)), int(m.group(2))


def parse_range(ref: str):
    m = RANGE_RE.match(ref)
    if m is None:
        raise ValueError('Invalid merged cell range: '+ref)
    return col_num(m.group(1)), int(m.group(2)), col_num(m.group(3)), int(m.group(4))


def xml_bytes(root):
    return ET.tostring(root, encoding='UTF-8', xml_declaration=True)


def load_strings(z):
    if 'xl/sharedStrings.xml' not in z.namelist():
        return []
    root = ET.fromstring(z.read('xl/sharedStrings.xml'))
    return [''.join(si.itertext()) for si in root.findall(f'{{{S}}}si')]


def read_sheet_cells(root, strings):
    data = {}
    for cell in root.iter(f'{{{S}}}c'):
        addr = cell.get('r')
        if not addr: continue
        v = cell.find(f'{{{S}}}v')
        inline = cell.find(f'{{{S}}}is')
        if v is None and inline is None:
            continue
        val = (v.text or '') if v is not None else ''.join(inline.itertext())
        if cell.get('t') == 's':
            try: val = strings[int(val)]
            except (ValueError, IndexError): val = ''
        data[addr] = str(val)
    return data


def sheet_descriptors(z):
    wb = ET.fromstring(z.read('xl/workbook.xml'))
    rels_root = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    targets = {r.get('Id'): r.get('Target') for r in rels_root}
    sheets = []
    for sh in wb.findall(f'.//{{{S}}}sheet'):
        rid = sh.get(f'{{{R}}}id')
        target = targets.get(rid)
        if not target: continue
        # Target may be /xl/worksheets/sheet1.xml or worksheets/sheet1.xml.
        path = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join('xl', target))
        sheets.append({'title': sh.get('name'), 'path': path})
    return sheets


def maybe_num(text):
    if text is None: return None
    try:
        n = int(float(str(text).strip()))
        if 1 <= n <= 10000 and float(str(text).strip()) == n:
            return n
    except (ValueError, TypeError): pass
    return None


def width_px(width):
    # Excel's approximate column width conversion, Calibri 11 default font.
    return max(1, int(float(width) * 7 + 5))


def slot_ratio(root, rect):
    left, top, right, bottom = rect
    cols = root.find(f'{{{S}}}cols')
    size_by_col = {}
    if cols is not None:
        for col in cols:
            if local(col.tag) != 'col': continue
            min_c, max_c = int(col.get('min',1)), int(col.get('max',1))
            if min_c > right or max_c < left: continue
            for i in range(max(min_c,left), min(max_c,right)+1):
                size_by_col[i] = float(col.get('width', 8.43))
    sheet_format = root.find(f'{{{S}}}sheetFormatPr')
    default_width = float(sheet_format.get('defaultColWidth','8.43')) if sheet_format is not None else 8.43
    default_height = float(sheet_format.get('defaultRowHeight','15')) if sheet_format is not None else 15
    px_width = sum(width_px(size_by_col.get(c,default_width)) for c in range(left,right+1))
    heights = {}
    sheet_data = root.find(f'{{{S}}}sheetData')
    for row in sheet_data:
        n = int(row.get('r',0))
        if top <= n <= bottom and row.get('ht'):
            heights[n] = float(row.get('ht'))
    px_height = sum(heights.get(n, default_height)*4/3 for n in range(top,bottom+1))
    return round(max(0.5,min(4,px_width/px_height)),5)


def photo_slots(root, strings):
    cells = read_sheet_cells(root, strings)
    merges_root = root.find(f'{{{S}}}mergeCells')
    merged = [parse_range(x.get('ref')) for x in merges_root] if merges_root is not None else []
    # Large merged areas designate the boxes for the photographs.
    rectangles = [(x1,y1,x2,y2) for x1,y1,x2,y2 in merged if x2-x1 >= 9 and y2-y1 >= 4]
    slots = {}
    for addr, value in cells.items():
        number = maybe_num(value)
        if number is None: continue
        col, row = address_parts(addr)
        # Template convention: image box starts 3 rows under the index, one column to the right.
        matches = [rect for rect in rectangles if rect[0] >= col and rect[0] <= col+2 and row+2 <= rect[1] <= row+6]
        if not matches: continue
        rect = min(matches, key=lambda r: (abs((r[1]-row)-3),abs((r[0]-col)-1)))
        slots[number] = {'rect': list(rect), 'ratio': slot_ratio(root, rect)}
    return slots


def examine_xlsx(contents: bytes):
    if len(contents) > 25*1024*1024: raise ValueError('엑셀 파일은 25MB 이하만 업로드할 수 있습니다.')
    try: z = zipfile.ZipFile(BytesIO(contents))
    except (zipfile.BadZipFile, ValueError): raise ValueError('정상적인 .xlsx 파일이 아닙니다.')
    with z:
        members = z.infolist()
        if len(members)>4000 or sum(x.file_size for x in members)>160*1024*1024:
            raise ValueError('엑셀 파일의 내부 크기가 너무 큽니다.')
        strings = load_strings(z)
        sheets = sheet_descriptors(z)
        if not sheets: raise ValueError('엑셀 시트가 없습니다.')
        slots = {}; photo_sheet = None
        for s in sheets:
            root = ET.fromstring(z.read(s['path']))
            candidates = photo_slots(root, strings)
            if len(candidates) >= 1 and len(candidates) > len(slots):
                slots = candidates; photo_sheet = s['path']
        if not photo_sheet:
            raise ValueError('사진 입력용 영역을 찾지 못했습니다. 같은 형식의 엑셀인지 확인해 주세요.')

        panels = []
        for s in sheets:
            if s['path'] == photo_sheet: continue
            root = ET.fromstring(z.read(s['path']))
            values = read_sheet_cells(root, strings)
            by_row = defaultdict(dict)
            for addr, value in values.items():
                col, row = address_parts(addr)
                by_row[row][col] = value
            header = None
            for rownum in sorted(by_row)[:18]:
                row = by_row[rownum]
                normalized = {c: re.sub(r'\s+', '',v).lower() for c,v in row.items()}
                floor = next((c for c,v in normalized.items() if '층수' in v), None)
                name = next((c for c,v in normalized.items() if '판넬명칭' in v or '패널명칭' in v or '장비명칭' in v), None)
                location = next((c for c,v in normalized.items() if '설치위치' in v or '실제위치' in v), None)
                date = next((c for c,v in normalized.items() if '점검일' in v or '측정일' in v), None)
                temp = next((c for c,v in normalized.items() if '최고온도' in v or '최고온도(' in v), None)
                if all(x is not None for x in [floor,name,location,date,temp]):
                    header = (rownum, floor,name,location,date,temp); break
            if header is None: continue
            header_row, floor_c,name_c,location_c,date_c,temp_c = header
            seq_c = min(by_row[header_row])
            for rownum in sorted(k for k in by_row if k > header_row):
                row = by_row[rownum]
                name = row.get(name_c,'').strip()
                if not name or name in ('-', '—'): continue
                number = maybe_num(row.get(seq_c))
                if number is None: continue
                if number not in slots:
                    # Don't silently export a reading without its original photo area.
                    continue
                slot = slots[number]
                panels.append({
                    'sheet_path':s['path'], 'sheet_title':s['title'],
                    'excel_row':rownum, 'ordinal':number,
                    'floor': row.get(floor_c,'').strip() or '층수 미기재',
                    'name': name, 'location': row.get(location_c,'').strip() or '위치 미기재',
                    'date_cell':col_label(date_c)+str(rownum),
                    'temperature_cell':col_label(temp_c)+str(rownum),
                    'original_date':row.get(date_c,'').strip(),
                    'original_temperature':row.get(temp_c,'').strip(),
                    'photo_rect':slot['rect'], 'photo_ratio':slot['ratio'],
                })
        if not panels:
            raise ValueError('층수/판넬명칭/설치위치/점검일/최고온도 열을 가진 관리대장을 찾지 못했습니다.')
        panels.sort(key=lambda p:(p['ordinal'],p['sheet_path']))
        return {'photo_sheet':photo_sheet, 'sheets':[s['title'] for s in sheets], 'panels': panels}


def set_number_or_text(root, addr, value, numeric=False):
    cell = root.xpath(f'.//m:sheetData/m:row/m:c[@r="{addr}"]', namespaces=NS)
    if not cell:
        col,rownum = address_parts(addr)
        data = root.find(f'{{{S}}}sheetData')
        row = next((x for x in data if int(x.get('r')) == rownum), None)
        if row is None:
            row = ET.Element(f'{{{S}}}row', r=str(rownum))
            data.append(row)
        cell = ET.Element(f'{{{S}}}c',r=addr)
        row.append(cell)
        cell = [cell]
    cell = cell[0]
    for ch in list(cell):
        if local(ch.tag) in ('v','is','f'):cell.remove(ch)
    if numeric:
        cell.attrib.pop('t',None)
        ET.SubElement(cell,f'{{{S}}}v').text = str(value)
    else:
        # Inline strings don't mutate shared strings and keep the original cell style.
        cell.set('t','inlineStr')
        is_ = ET.SubElement(cell,f'{{{S}}}is')
        ET.SubElement(is_,f'{{{S}}}t').text = str(value)


def make_photo_anchor(panel, relationship_id, picture_id):
    x1,y1,x2,y2 = panel['photo_rect']
    two = ET.Element(f'{{{XDR}}}twoCellAnchor',editAs='oneCell')
    for tag,col,row in [('from',x1-1,y1-1),('to',x2,y2)]:
        el=ET.SubElement(two,f'{{{XDR}}}{tag}')
        for key,value in [('col',col),('colOff',0),('row',row),('rowOff',0)]:
            ET.SubElement(el,f'{{{XDR}}}{key}').text=str(value)
    pic=ET.SubElement(two,f'{{{XDR}}}pic')
    nv=ET.SubElement(pic,f'{{{XDR}}}nvPicPr')
    ET.SubElement(nv,f'{{{XDR}}}cNvPr',id=str(picture_id),name=f"Inspection photo {panel['ordinal']}")
    cnv=ET.SubElement(nv,f'{{{XDR}}}cNvPicPr')
    ET.SubElement(cnv,f'{{{A}}}picLocks',noChangeAspect='1')
    fill=ET.SubElement(pic,f'{{{XDR}}}blipFill')
    ET.SubElement(fill,f'{{{A}}}blip',{f'{{{R}}}embed':relationship_id})
    stretch=ET.SubElement(fill,f'{{{A}}}stretch')
    ET.SubElement(stretch,f'{{{A}}}fillRect')
    shape=ET.SubElement(pic,f'{{{XDR}}}spPr')
    geom=ET.SubElement(shape,f'{{{A}}}prstGeom',prst='rect')
    ET.SubElement(geom,f'{{{A}}}avLst')
    ET.SubElement(two,f'{{{XDR}}}clientData')
    return two


def to_jpeg(cropped_path, ratio):
    with Image.open(cropped_path) as img:
        img=ImageOps.exif_transpose(img).convert('RGB')
        # The browser crops first. Normalize again to avoid pixel rounding drift.
        w,h=img.size
        if abs(w/h-ratio)>0.009:
            if w/h > ratio:
                wanted = int(round(h*ratio)); left=(w-wanted)//2
                img = img.crop((left,0,left+wanted,h))
            else:
                wanted=int(round(w/ratio)); top=(h-wanted)//2
                img = img.crop((0,top,w,top+wanted))
        if img.width>1400:
            img.thumbnail((1400,1400))
        out=BytesIO();img.save(out,format='JPEG',quality=90,optimize=True)
        return out.getvalue()


def export_xlsx(template: bytes, panels: list, readings: dict, photo_sheet: str):
    """Returns XLSX bytes. readings: ordinal -> {'date','temperature','photo_path'}.

    Uses OOXML surgical patching; preserves existing sheet formatting, page settings,
    merged ranges, charts, formulas, cell styles, and pre-existing ZIP parts.
    """
    with zipfile.ZipFile(BytesIO(template)) as src:
        names=set(src.namelist())
        modified={}
        targets=defaultdict(list)
        photo_panels=[]
        for panel in panels:
            reading=readings.get(panel['ordinal'])
            if not reading: continue
            targets[panel['sheet_path']].append((panel, reading))
            if reading.get('photo_path') and os.path.isfile(reading['photo_path']):
                photo_panels.append((panel,reading))
        for path, entries in targets.items():
            root=ET.fromstring(src.read(path))
            for panel,reading in entries:
                set_number_or_text(root,panel['date_cell'],reading['date'])
                set_number_or_text(root,panel['temperature_cell'],reading['temperature'],numeric=True)
            modified[path]=xml_bytes(root)

        if photo_panels:
            photo_root=ET.fromstring(modified.get(photo_sheet,src.read(photo_sheet)))
            sheet_base=posixpath.basename(photo_sheet)
            sheet_rels_path=f"{posixpath.dirname(photo_sheet)}/_rels/{sheet_base}.rels"
            relroot=ET.fromstring(src.read(sheet_rels_path)) if sheet_rels_path in names else ET.Element(f'{{{P}}}Relationships')
            existing_ids={r.get('Id') for r in relroot}
            drawing_rel=next((r for r in relroot if r.get('Type','').endswith('/drawing')),None)
            if drawing_rel is not None:
                drawing_path=posixpath.normpath(posixpath.join(posixpath.dirname(photo_sheet),drawing_rel.get('Target').lstrip('/'))) if not drawing_rel.get('Target').startswith('/') else drawing_rel.get('Target').lstrip('/')
                drawing_root=ET.fromstring(src.read(drawing_path))
            else:
                n=1
                while f'xl/drawings/drawing{n}.xml' in names:n+=1
                drawing_path=f'xl/drawings/drawing{n}.xml'
                drawing_root=ET.Element(f'{{{XDR}}}wsDr',nsmap={'xdr':XDR,'a':A,'r':R})
                i=1
                while f'rId{i}' in existing_ids:i+=1
                draw_id=f'rId{i}'
                ET.SubElement(relroot,f'{{{P}}}Relationship',Id=draw_id,Type=f'{R}/drawing',Target=f'../drawings/drawing{n}.xml')
                # The drawing element is placed after pageSetup / drawing-related nodes.
                drawing_el=ET.Element(f'{{{S}}}drawing',{f'{{{R}}}id':draw_id})
                before=next((i for i,child in enumerate(photo_root) if local(child.tag) in ('legacyDrawing','legacyDrawingHF','picture','oleObjects','controls','webPublishItems','tableParts','extLst')),len(photo_root))
                photo_root.insert(before,drawing_el)
                existing_ids.add(draw_id)
            drawing_filename=posixpath.basename(drawing_path)
            drawing_rels_path=f"{posixpath.dirname(drawing_path)}/_rels/{drawing_filename}.rels"
            draw_rels=ET.fromstring(src.read(drawing_rels_path)) if drawing_rels_path in names else ET.Element(f'{{{P}}}Relationships')
            occupied_ids={r.get('Id') for r in draw_rels}
            existing_pic_ids=[int(n.get('id')) for n in drawing_root.iter(f'{{{XDR}}}cNvPr') if n.get('id','').isdigit()]
            next_pic=max(existing_pic_ids,default=0)+1
            photo_files={}
            for panel, reading in photo_panels:
                i=1
                while f'rId{i}' in occupied_ids:i+=1
                rid=f'rId{i}';occupied_ids.add(rid)
                image_idx=1
                while f'xl/media/inspection_{image_idx}.jpg' in names or f'xl/media/inspection_{image_idx}.jpg' in photo_files:image_idx+=1
                picpath=f'xl/media/inspection_{image_idx}.jpg'
                photo_files[picpath]=to_jpeg(reading['photo_path'],panel['photo_ratio'])
                relative=posixpath.relpath(picpath,posixpath.dirname(drawing_path))
                ET.SubElement(draw_rels,f'{{{P}}}Relationship',Id=rid,Type=f'{R}/image',Target=relative)
                drawing_root.append(make_photo_anchor(panel,rid,next_pic))
                next_pic+=1
            modified[drawing_path]=xml_bytes(drawing_root)
            modified[drawing_rels_path]=xml_bytes(draw_rels)
            modified[sheet_rels_path]=xml_bytes(relroot)
            modified[photo_sheet]=xml_bytes(photo_root)
            modified.update(photo_files)
            content_root=ET.fromstring(src.read('[Content_Types].xml'))
            if not any(x.get('Extension','').lower() in ('jpg','jpeg') for x in content_root if local(x.tag)=='Default'):
                ET.SubElement(content_root,f'{{{CT}}}Default',Extension='jpg',ContentType='image/jpeg')
            elif not any(x.get('Extension')=='jpg' for x in content_root if local(x.tag)=='Default'):
                ET.SubElement(content_root,f'{{{CT}}}Default',Extension='jpg',ContentType='image/jpeg')
            if not any(x.get('PartName')=='/'+drawing_path for x in content_root):
                ET.SubElement(content_root,f'{{{CT}}}Override',PartName='/'+drawing_path,ContentType='application/vnd.openxmlformats-officedocument.drawing+xml')
            modified['[Content_Types].xml']=xml_bytes(content_root)

        result=BytesIO()
        with zipfile.ZipFile(result,'w') as dst:
            for inf in src.infolist():
                dst.writestr(inf,modified.pop(inf.filename,src.read(inf.filename)))
            for path,data in modified.items():
                dst.writestr(path,data,compress_type=zipfile.ZIP_DEFLATED)
        return result.getvalue()
