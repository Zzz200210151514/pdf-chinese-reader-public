import unittest
from test_parser_layout import inspection, block, line
from core.classifier import classify
from llm.freeze import audit, freeze, restore

class WorkflowRegressions(unittest.TestCase):
    def test_native_object_cannot_be_removed_from_validation_scope(self):
        import tempfile
        from pathlib import Path
        import pymupdf as fitz
        from core.validator import validate
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.pdf'
            with fitz.open() as doc:
                doc.new_page();doc[0].insert_text((40,50),'*');doc.save(path)
            mapping={'blocks':[{'id':'native','source_page':1,'rect':[40,35,55,55],
                                'translation_required':False,'source_text':'*'}]}
            manifest={'pages':[{'source_page':1,'regions':[],'protected':[]}]}
            self.assertFalse(validate(path,path,manifest,mapping)['passed'])

    def test_charge_spacing_is_equivalent_but_charge_loss_is_not(self):
        source='OH − ions at 10 mM'
        frozen,tokens=freeze(source)
        translated=restore(frozen.replace(' − ions','−离子'),tokens)
        self.assertEqual(audit(source,translated),{'missing':[],'added':[]})
        self.assertTrue(audit(source,'OH 离子 at 10 mM')['missing'])

    def test_publisher_toolbar_is_native(self):
        m=classify(inspection([block('ACCESS\nMetrics & More\nArticle Recommendations',[40,100,550,116])]))
        self.assertFalse(m['blocks'][0]['translation_required'])

    def test_repeated_footer_line_in_different_blocks_is_native(self):
        data=inspection([block('',[40,740,550,790],lines=[line('Article body ends here.',[40,740,200,753]),line('Journal 2024, 4, 1063−1072',[400,775,550,781])])])
        import copy
        second=copy.deepcopy(data['pages'][0]);second['source_page']=2
        data['pages'].append(second);data['page_count']=2
        m=classify(data)
        footer=[b for b in m['blocks'] if b['source_text']=='Journal 2024, 4, 1063−1072']
        self.assertEqual(len(footer),2)
        self.assertTrue(all(not b['translation_required'] for b in footer))

    def test_equation_cluster_includes_textual_numerator(self):
        data=inspection([
            block('zzzzz',[60,160,70,190],lines=[line('zzzzz',[60,160,70,190],spans=[{'text':'zzzzz','bbox':[60,160,70,190],'font':'Mathematica2','size':10}])]),
            block('initial conductivity of dilute',[90,175,240,185]),
            block('= × 100%',[80,170,260,192]),block('(1)',[280,180,292,192]),
            block('where the conductivity is measured.',[40,205,290,220])])
        m=classify(data)
        for b in m['blocks']:
            if b['source_text'].startswith('where'):self.assertTrue(b['translation_required'])
            else:self.assertFalse(b['translation_required'],b['source_text'])

if __name__=='__main__':unittest.main()
