import importlib.util
from pathlib import Path
import unittest

from app.domain.translation_numeric_audit import numeric_facts, missing_numeric_facts
from app.domain.translation_quality_audit import audit_translation_chunk


class TemporalNumericTests(unittest.TestCase):
    def test_explicit_decades_and_centuries(self):
        for source, target in [
            ('1980s', '20世纪80年代'), ('1980’s', '二十世纪八十年代'),
            ("1980's", '一九八〇年代'), ('1980s', '1980年代'),
            ('2000s', '21世纪00年代'), ('2010s', '二十一世纪一十年代'),
            ('19th century', '19世纪'), ('20th century CE', '公元20世纪'),
            ('19th century B.C.E.', '公元前十九世纪'),
            ('1980s BC', '公元前1980年代'), ('1980s', '１９８０年代'),
            ('early 1980s', '20世纪80年代初期'), ('mid-1980s', '20世纪80年代中期'),
            ('late 1980s', '20世纪80年代末'), ('early in the 1980s', '1980年代早期'),
            ('late 19th century', '十九世纪晚期'),
            ('21st-century', '21世纪'), ('mid‑1980s', '1980年代中期'),
            ('early 21st‐century', '21世纪初期'), ('BC 1980s', '公元前1980年代'),
            ('B.C.E. 19th century', '公元前19世纪'), ('AD 1980s', '1980年代'),
            ('880s', '九世纪八十年代'), ('140s BCE', '公元前二世纪四十年代'),
            ('9990s', '100世纪90年代'), ('100th century', '100世纪'),
        ]:
            with self.subTest(source=source, target=target):
                self.assertEqual(missing_numeric_facts(source, target), [])
                self.assertEqual(missing_numeric_facts(target, source), [])

    def test_wrong_time_unit_era_or_qualifier_is_not_equivalent(self):
        for source, target in [
            ('1980s', '1980年'), ('1980', '1980年代'), ('1980s', '80年代'),
            ('1980s', '上世纪80年代'), ('1980s', '20世纪90年代'),
            ('1980s', '19世纪80年代'), ('19th century', '20世纪'),
            ('19th century BC', '19世纪'), ('1980s BCE', '1980年代'),
            ('1980s', '公元前1980年代'), ('early 1980s', '1980年代末期'),
            ('late 1980s', '1980年代'), ('1980s', '1980年代初'),
            ('1980s and 1980s', '1980年代'), ('1980s and 1980', '1980年代'),
            ('19th century', '第19章'), ('1980 dollars', '1980年代'),
            ('19st century', '19世纪'), ('BC 1980s AD', '1980年代'),
            ('BC 1980s AD', '公元前1980年代'), ('mid‑1980s', '1980年代末期'),
            ('early to mid 1980s', '1980年代中期'),
            ('early to late 19th century', '19世纪末期'),
            ('20世纪80年代初至中期', 'early 1980s'),
        ]:
            with self.subTest(source=source, target=target):
                self.assertTrue(missing_numeric_facts(source, target))

    def test_parameterized_unseen_dates(self):
        # A grid of dates, not examples selected from a particular book.
        for year in range(100, 10000, 10):
            c, d = year // 100 + 1, year % 100
            source, target = f'{year}s', f'{c}世纪{d:02d}年代'
            self.assertEqual(missing_numeric_facts(source, target), [])
            self.assertTrue(missing_numeric_facts(source, f'{c + 1}世纪{d:02d}年代'))
            self.assertTrue(missing_numeric_facts(source, f'{year}年'))

    def test_decade_is_one_fact_and_preserves_spans(self):
        text = 'In the late 1980s, there were 12 people, 12 people and 10 percent growth.'
        facts = numeric_facts(text)
        self.assertEqual([f.kind for f in facts], ['decade:ce:late', 'number', 'number', 'percent'])
        for fact in facts:
            self.assertEqual(text[fact.start:fact.end].strip(), fact.raw.strip())
        self.assertEqual(missing_numeric_facts(text, '20世纪80年代末，有12人、12人，增长10%。'), [])
        self.assertTrue(missing_numeric_facts(text, '20世纪80年代末，有12人，增长10%。'))

    def test_non_temporal_units_keep_negative_checks(self):
        for source, target in [('1980%', '1980'), ('$1980', '1980年'),
                               ('-1980', '1980'), ('1.980', '1980'),
                               ('1980 people and 1980s', '1980年代'),
                               ('19 to 20 percent', '19%到21%')]:
            self.assertTrue(missing_numeric_facts(source, target))

    def test_temporal_rules_do_not_expand_abbreviated_or_invalid_dates(self):
        for text in ["'80s", '80年代', 'ModelX1980s', '1985s', '0th century', '1000th century']:
            self.assertFalse(any(f.kind.startswith(('decade:', 'century:')) for f in numeric_facts(text)))

    def test_html_inline_digits_and_note_refs_are_separate(self):
        source = '<p>In the 1980s<sup>1</sup>, 10 percent improved.</p>'
        target = '<p>在20世纪80年代<sup>1</sup>，10%有所改善。</p>'
        self.assertNotIn('numbers_missing', audit_translation_chunk(original_html=source, translated_html=target).flags)
        self.assertIn('numbers_missing', audit_translation_chunk(original_html=source, translated_html=target.replace('80年代','90年代')).flags)

    def test_real_history_negative_validator_checks_the_selected_value(self):
        import app.domain.translation_numeric_audit as module
        path = Path(__file__).resolve().parents[1] / 'scripts/regression-numeric-history.py'
        spec = importlib.util.spec_from_file_location('numeric_history_tests', path)
        history = importlib.util.module_from_spec(spec); spec.loader.exec_module(history)
        result = history.corruption_checks(module, '10 percent in the 1980s', '1980年代，10%即百分之十。')
        self.assertEqual(result['percent'], 1)
        self.assertEqual(result['decade:ce:whole'], 1)


if __name__ == '__main__':
    unittest.main()
