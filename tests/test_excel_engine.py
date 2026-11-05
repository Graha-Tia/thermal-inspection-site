"""Tests run with: python -m unittest discover -s tests -v"""
from io import BytesIO
from pathlib import Path
import unittest
import zipfile

from PIL import Image
from lxml import etree as ET

from excel_engine import examine_xlsx, export_xlsx

S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
XDR = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / 'sample' / '분전반리스트_원본.xlsx'


class SpreadsheetPreservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = ORIGINAL.read_bytes()
        cls.info = examine_xlsx(cls.template)

    def test_list_of_panels_matches_both_sheets(self):
        panels = self.info['panels']
        self.assertEqual(len(panels), 50)
        self.assertEqual(panels[0]['name'], 'LU-A1')
        self.assertEqual(panels[0]['photo_rect'], [2, 8, 27, 25])
        self.assertEqual(panels[46]['name'], '튀김기A')
        self.assertEqual(panels[46]['temperature_cell'], 'H4')
        self.assertEqual(panels[-1]['name'], '전기온수기')
        self.assertEqual(len({p['ordinal'] for p in panels}), 50)
        self.assertAlmostEqual(panels[0]['photo_ratio'], 1.59, places=2)

    def test_unmeasured_export_keeps_all_file_content(self):
        output=export_xlsx(self.template,self.info['panels'],{},self.info['photo_sheet'])
        with zipfile.ZipFile(BytesIO(self.template)) as a, zipfile.ZipFile(BytesIO(output)) as b:
            self.assertEqual(set(a.namelist()),set(b.namelist()))
            for filename in a.namelist():
                self.assertEqual(a.read(filename),b.read(filename),filename)

    def test_populated_export_only_updates_target_cells_and_adds_photos(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temp_dir:
            photo_path=Path(temp_dir) / 'heat.jpg'
            Image.new('RGB',(980,600),(210,50,15)).save(photo_path)
            readings={
                1: {'date':'2026-11-05','temperature':42.5,'photo_path':str(photo_path)},
                47:{'date':'2026-11-05','temperature':52.3,'photo_path':str(photo_path)},
            }
            output=export_xlsx(self.template,self.info['panels'],readings,self.info['photo_sheet'])
            with zipfile.ZipFile(BytesIO(self.template)) as base, zipfile.ZipFile(BytesIO(output)) as out:
                self.assertIsNone(out.testzip())
                self.assertIn('xl/drawings/drawing1.xml',out.namelist())
                self.assertIn('xl/media/inspection_1.jpg',out.namelist())
                self.assertIn('xl/media/inspection_2.jpg',out.namelist())
                # Neither the original styles nor the original merged ranges change.
                self.assertEqual(base.read('xl/styles.xml'),out.read('xl/styles.xml'))
                self.assertEqual(base.read('xl/workbook.xml'),out.read('xl/workbook.xml'))
                self.assertEqual(base.read('xl/sharedStrings.xml'),out.read('xl/sharedStrings.xml'))
                self.assertEqual(base.read('xl/worksheets/sheet2.xml.rels') if 'xl/worksheets/sheet2.xml.rels' in base.namelist() else b'', b'')
                for i,cell_name,value in ((1,'E4','2026-11-05'),(1,'F4','42.5'),(2,'E4','2026-11-05'),(2,'H4','52.3')):
                    root=ET.fromstring(out.read(f'xl/worksheets/sheet{i}.xml'))
                    cell=root.xpath(f'.//m:c[@r="{cell_name}"]',namespaces={'m':S})[0]
                    self.assertEqual(''.join(cell.itertext()),value)
                drawing=ET.fromstring(out.read('xl/drawings/drawing1.xml'))
                anchors=drawing.findall(f'{{{XDR}}}twoCellAnchor')
                self.assertEqual(len(anchors),2)
                self.assertEqual(anchors[0].find(f'{{{XDR}}}from/{{{XDR}}}col').text,'1')
                self.assertEqual(anchors[0].find(f'{{{XDR}}}from/{{{XDR}}}row').text,'7')
                self.assertEqual(anchors[1].find(f'{{{XDR}}}from/{{{XDR}}}col').text,'59')
                self.assertEqual(anchors[1].find(f'{{{XDR}}}from/{{{XDR}}}row').text,'282')
                # Reload exported workbook into parser, including the existing DrawingML.
                result=examine_xlsx(output)
                self.assertEqual(len(result['panels']),50)
                self.assertEqual(result['panels'][0]['original_temperature'],'42.5')
                # Re-export after adding new measurement retains existing drawings.
                output2=export_xlsx(output,result['panels'],{2:readings[1]},result['photo_sheet'])
                with zipfile.ZipFile(BytesIO(output2)) as out2:
                    drawing2=ET.fromstring(out2.read('xl/drawings/drawing1.xml'))
                    self.assertEqual(len(drawing2.findall(f'{{{XDR}}}twoCellAnchor')),3)

    def test_new_project_same_layout_with_different_floor(self):
        from excel_engine import set_number_or_text,xml_bytes
        with zipfile.ZipFile(BytesIO(self.template)) as source:
            changed=BytesIO()
            with zipfile.ZipFile(changed,'w') as dest:
                for info in source.infolist():
                    raw=source.read(info.filename)
                    if info.filename=='xl/worksheets/sheet1.xml':
                        root=ET.fromstring(raw)
                        set_number_or_text(root,'B4','C동 8F')
                        set_number_or_text(root,'D4','전기실')
                        raw=xml_bytes(root)
                    dest.writestr(info,raw)
        panels=examine_xlsx(changed.getvalue())['panels']
        self.assertEqual(panels[0]['floor'],'C동 8F')
        self.assertEqual(panels[0]['location'],'전기실')
        self.assertEqual(len(panels),50)


if __name__=='__main__': unittest.main()
