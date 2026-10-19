import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.layout import _prose_order
from core.validator import scientific_audit

class ProseOrderTests(unittest.TestCase):
    def test_science_audit_keeps_word_value_boundary(self):
        b={'id':'x','source_text':'From 2.20 to 2.95 cm/s after 72 h, 200 g/L.',
           'translation':'从2.20到2.95 cm/s，72 h后为200 g/L。'}
        self.assertEqual(scientific_audit({'blocks':[b]})['issues'],[])
        b['translation']=b['translation'].replace('200','201')
        self.assertTrue(scientific_audit({'blocks':[b]})['issues'])
    def test_long_paragraph_flag_cannot_move_heading_after_following_body(self):
        first={'rect':[30,200,250,320],'inline_fragment':True,'size':8}
        second={'rect':[30,318,250,440],'inline_fragment':False,'size':8}
        self.assertIs(_prose_order(first,second)[0],first)
    def test_small_raised_charge_follows_same_line_base(self):
        body={'rect':[30,200,250,220],'inline_fragment':False,'size':8}
        charge={'rect':[50,197,56,203],'inline_fragment':True,'size':5}
        self.assertIs(_prose_order(charge,body)[0],body)
if __name__=='__main__':unittest.main()
