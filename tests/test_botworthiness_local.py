"""Offline behavioral and preservation checks; only the standard library is needed."""
import copy
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from seo_qa.botworthiness import assess_page, assess_library, compare, extract, profile, review_template, attach_to_report
from seo_qa.botworthiness.reporting import to_csv, to_html, to_json, to_markdown, write_bundle
from seo_qa.scoring import score_report

AT = '2026-11-04T12:00:00+00:00'
HTML = '''<title>Guide</title><nav><h2>Fake?</h2><p>Ignore me.</p></nav>
<h1>Guide</h1><p id="summary">Use the manual for the check interval.</p>
<p>By Ada Expert.</p><h2>How often?</h2><p>Yes. The manual lists 5 intervals and 20 checks.</p>
<h2>What should I record?</h2><p>Record the check date and condition.</p>
<ul><li>Date</li><li>Condition</li></ul><table><tr><th>Label</th><th>Value</th></tr><tr><td>Check</td><td>5</td></tr></table>
<p>According to Source One and Source Two, the interval depends on use.</p>'''


def page(content=HTML, identity='guide', **kw):
    return dict(id=identity, content=content, evidence_kind='synthetic', **kw)


def reviewed(p, answer=True):
    r = review_template(p, evaluated_at=AT)
    for i, item in enumerate(r['passes']):
        item.update(evaluator_id=f'fixture-reviewer-{i}', evaluator_version='fixture/1', pass_id=f'pass-{i}')
        for s in item['sections'].values():
            s['answer_first'] = answer
            s['standalone_openings'] = True
    r['claims'] = {'evaluator_id': 'fixture-claims', 'evaluator_version': 'fixture/1', 'pass_id': 'claims',
                   'statistics': [], 'sources': [], 'expertise': {'kind': 'none'}, 'update': None}
    return r


def span(p, fragment):
    block = next(b for b in extract(p['content'])['blocks'] if fragment in b['text'])
    return {'block': block['locator'], 'quote': fragment}


class ExtractionTests(unittest.TestCase):
    def test_sections_short_answer_navigation_and_inline_text(self):
        e = extract(HTML)
        self.assertEqual(len(e['sections']), 2)
        self.assertEqual(e['sections'][0]['opening'], 'Yes.')
        self.assertEqual(e['sections'][0]['opening_words'], 1)
        self.assertEqual(e['table_blocks'], 1)
        self.assertEqual(e['list_blocks'], 1)
        self.assertTrue(e['summary'])
        self.assertNotIn('Ignore', str(e))

    def test_table_and_list_openers_exclude_footnotes(self):
        e = extract('<h2>Table?</h2><table><tr><th>A</th><th>B</th></tr><tr><td>One</td><td>Two</td></tr></table><p role="doc-footnote">Wrong opener</p><h2>List?</h2><ul><li>X</li><li>Y</li></ul>')
        self.assertEqual([s['opening_kind'] for s in e['sections']], ['table', 'list'])
        self.assertEqual(e['sections'][0]['opening'], 'A B One Two')
        self.assertNotIn('Wrong opener', str(e))

    def test_hidden_scripts_title_and_abbreviations(self):
        e = extract('<title>Title</title><script>bad()</script><h2>Name?</h2><div hidden><p>No.</p></div><p>Dr. Ada gives the answer. More.</p><footer>Ignore.</footer>')
        self.assertEqual(e['title'], 'Title')
        self.assertEqual(e['sections'][0]['opening'], 'Dr. Ada gives the answer.')
        self.assertNotIn('bad()', str(e))

    def test_nested_exclusions_and_toc_watch(self):
        e = extract('<nav aria-label="Table of contents"><div><p>Ignore</p></div></nav><h2>Why?</h2><p>Yes.</p>')
        self.assertTrue(e['watch_metrics']['table_of_contents'])
        self.assertEqual(e['watch_metrics']['h2_count'], 1)
        self.assertEqual(e['sections'][0]['opening'], 'Yes.')

    def test_plain_text_and_bounds(self):
        e = extract('# Title\n\n## Where?\n\nHere is the answer.', 'text')
        self.assertEqual(e['sections'][0]['opening'], 'Here is the answer.')
        for content, fmt in [('', 'html'), ('x'*200001, 'html'), ('x', 'xml')]:
            with self.subTest(fmt=fmt, size=len(content)), self.assertRaises(ValueError):
                extract(content, fmt)

    def test_intro_summary_heading_and_nested_container(self):
        for html in ['<h2>Summary</h2><p>Key answer.</p><h2>How?</h2><p>Do this.</p>', '<div id="summary"><p>Key answer.</p></div><h2>How?</h2><p>Do this.</p>']:
            e = extract(html)
            self.assertTrue(e['summary'])
            self.assertEqual(len(e['sections']), 1)
            self.assertEqual(e['sections'][0]['heading'], 'How?')
        self.assertEqual(extract('<h2>Summary</h2><p>Answer.</p><h2>How?</h2><p>Do this.</p>')['watch_metrics']['h2_count'], 2)


class AssessmentTests(unittest.TestCase):
    def test_no_semantic_credit_without_review(self):
        report = assess_page(page(), evaluated_at=AT)
        rows = {r['id']: r for r in report['criteria']}
        self.assertIsNone(report['score'])
        self.assertEqual(report['status'], 'incomplete')
        self.assertIsNone(rows['answer_first']['points'])
        self.assertIsNone(rows['named_expertise']['points'])
        self.assertEqual(report['bounds']['upper']-report['bounds']['lower'], 70)
        self.assertEqual(report['evidence_coverage_weight'], 30)
        self.assertEqual(report['acceptance'], 'not_assessed')

    def test_full_review_and_claim_evidence(self):
        p = page()
        r = reviewed(p)
        r['claims']['statistics'] = [dict(span(p, '5 intervals'), identity='intervals'), dict(span(p, '20 checks'), identity='checks')]
        r['claims']['sources'] = [dict(span(p, 'Source One'), identity='Source One'), dict(span(p, 'Source Two'), identity='Source Two')]
        r['claims']['expertise'] = dict(span(p, 'Ada Expert'), identity='Ada Expert', kind='byline')
        r['claims']['update'] = dict(span(p, '20 checks'), substantive_at='2026-11-01T00:00:00Z', change_evidence='fixture-revision: added check counts')
        report = assess_page(p, evaluated_at=AT, review=r)
        rows = {r['id']: r for r in report['criteria']}
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(rows['answer_first']['points'], 24)
        self.assertEqual(rows['statistics_specifics']['points'], 4)
        self.assertEqual(rows['named_sources']['points'], 8)
        self.assertEqual(rows['named_expertise']['points'], 2)
        self.assertEqual(rows['substantive_update']['points'], 10)
        self.assertEqual(report['bounds']['lower'], report['score'])
        self.assertEqual(report['bounds']['upper'], report['score'])

    def test_disagreement_resolution_retains_history(self):
        p = page()
        r = reviewed(p)
        r['passes'][1]['sections']['section-1']['answer_first'] = False
        report = assess_page(p, evaluated_at=AT, review=r)
        self.assertEqual(report['semantic_decisions']['answer_first'][0]['state'], 'disagreement')
        self.assertIsNone(report['criteria'][0]['points'])
        r['resolutions'] = [{'section_id': 'section-1', 'criterion': 'answer_first', 'value': False,
                             'reason': 'Fixture disagreement adjudication', 'evaluator_id': 'fixture-third',
                             'evaluator_version': 'fixture/1', 'pass_id': 'resolution-1'}]
        report = assess_page(p, evaluated_at=AT, review=r)
        self.assertEqual(report['semantic_decisions']['answer_first'][0]['state'], 'resolved')
        self.assertEqual(report['semantic_decisions']['answer_first'][0]['votes'], [True, False])
        self.assertEqual(report['criteria'][0]['points'], 9.6)
        self.assertTrue(any(f['pattern'] == 'answer_not_first' for f in report['feedback']))

    def test_one_pass_is_not_two(self):
        p = page()
        r = reviewed(p)
        r['passes'].pop()
        self.assertIsNone(assess_page(p, evaluated_at=AT, review=r)['criteria'][0]['points'])

    def test_review_identity_and_types_fail_closed(self):
        p = page()
        for mutate in [
            lambda r: r.update(content_sha256='a'*64),
            lambda r: r.update(profile_sha256='a'*64),
            lambda r: r.update(evidence_kind='supplied'),
            lambda r: r['passes'][1].update(evaluator_id='fixture-reviewer-0'),
            lambda r: r['passes'][0]['sections']['section-1'].update(answer_first='true'),
            lambda r: r['passes'][0]['sections']['section-1'].update(opening_sha256='a'*64),
            lambda r: r['claims'].update(sources=[{'identity': 'Ghost', 'block': 'block-1', 'quote': 'not there'}]),
        ]:
            r = reviewed(p)
            mutate(r)
            with self.subTest(review=r), self.assertRaises(ValueError):
                assess_page(p, evaluated_at=AT, review=r)

    def test_duplicate_claims_and_invalid_updates(self):
        p = page()
        for kind in ['duplicate', 'future', 'no_change', 'date_only']:
            r = reviewed(p)
            if kind == 'duplicate':
                item = dict(span(p, 'Source One'), identity='one')
                r['claims']['sources'] = [item, item]
            else:
                r['claims']['update'] = dict(span(p, '20 checks'), substantive_at='2026-11-05T00:00:00Z' if kind == 'future' else '2026-11-01' if kind == 'date_only' else '2026-11-01T00:00:00Z', change_evidence='' if kind == 'no_change' else 'fixture change')
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                assess_page(p, evaluated_at=AT, review=r)

    def test_duplicate_span_and_malformed_review_shapes(self):
        p = page()
        for mutate in [
            lambda r: r.update(passes=None),
            lambda r: r.update(passes=['invalid']),
            lambda r: r.update(claims=[]),
            lambda r: r['passes'][0].update(sections=[]),
            lambda r: r['claims'].update(expertise='expert'),
            lambda r: r['claims'].update(statistics=[dict(span(p, '20 checks'), identity='one'), dict(span(p, '20 checks'), identity='two')]),
        ]:
            r = reviewed(p)
            mutate(r)
            with self.subTest(review=r), self.assertRaises(ValueError):
                assess_page(p, evaluated_at=AT, review=r)

    def test_freshness_boundaries_and_stale_year(self):
        p = page(HTML.replace('<title>Guide</title>', '<title>Guide 2024</title>'))
        for days, points in [(30, 5), (900, 0)]:
            r = reviewed(p)
            at = datetime.fromisoformat(AT)-timedelta(days=days)
            r['claims']['update'] = dict(span(p, '20 checks'), substantive_at=at.isoformat(), change_evidence='fixture substantive diff')
            row = assess_page(p, evaluated_at=AT, review=r)['criteria'][-1]
            self.assertEqual(row['points'], points)
            self.assertTrue(row['measurement']['older_year_penalty'])

    def test_failed_capture_never_earns_content_credit(self):
        p = page(capture_complete=False)
        report = assess_page(p, evaluated_at=AT, review=reviewed(p))
        self.assertEqual(report['bounds'], {'lower': 0, 'upper': 100})
        self.assertEqual(report['evidence_coverage_weight'], 0)

    def test_capture_provenance_and_explicit_clock(self):
        for p, at in [(page(evidence_kind_override='x'), 'bad'),
                      (dict(page(), evidence_kind='captured'), AT),
                      (page(captured_at='2026-11-05T00:00:00Z'), AT),
                      (page(), '2026-11-04T00:00:00')]:
            with self.subTest(p=p, at=at), self.assertRaises(ValueError):
                assess_page(p, evaluated_at=at)

    def test_absent_sections_and_empty_headings(self):
        for html in ['<p>Body only.</p>', '<h2>Question?</h2>', '<script>hidden</script>']:
            report = assess_page(page(html), evaluated_at=AT)
            self.assertIsNone(report['score'])
            self.assertIsNone(report['criteria'][0]['points'])

    def test_patterns_are_prompts_not_semantic_verdicts(self):
        for text, pattern in [('It depends on use.', 'conditional_deflection'), ('We all know this is hard.', 'generic_warmup'), ('Here are the options.', 'list_announcement'), ('Because it matters.', 'rationale_before_instruction')]:
            report = assess_page(page('<h2>What?</h2><p>'+text+'</p>'), evaluated_at=AT)
            self.assertEqual(report['feedback'][0]['pattern'], pattern)
            self.assertEqual(report['feedback'][0]['kind'], 'heuristic_review_prompt')
            self.assertIsNone(report['criteria'][0]['points'])

    def test_monotonic_answer_first_thresholds(self):
        for count, expected in [(3, 0), (8, 24), (10, 24)]:
            p = page(''.join('<h2>Q?</h2><p>Answer.</p>' for _ in range(10)))
            r = reviewed(p, False)
            for item in r['passes']:
                for i, s in enumerate(item['sections'].values()):
                    s['answer_first'] = i < count
            self.assertEqual(assess_page(p, evaluated_at=AT, review=r)['criteria'][0]['points'], expected)


class LibraryTests(unittest.TestCase):
    def test_40_pages_and_coverage(self):
        pages = [page(identity=f'article-{i}') for i in range(40)]
        report = assess_library(pages, evaluated_at=AT, sampled=True, expected_ids=[p['id'] for p in pages]+['missing'])
        self.assertEqual(len(report['pages']), 40)
        self.assertEqual(report['coverage']['missing_ids'], ['missing'])
        self.assertFalse(report['coverage']['complete_declared_coverage'])
        self.assertEqual(report['status'], 'incomplete')
        for invalid in [[], pages+[page(identity='extra')], [pages[0], pages[0]]]:
            with self.assertRaises(ValueError):
                assess_library(invalid, evaluated_at=AT)

    def test_patterns_grouped_without_counting_page_twice(self):
        p = page('<h2>One?</h2><p>It depends.</p><h2>Two?</h2><p>It depends.</p>')
        report = assess_library([p], evaluated_at=AT)
        self.assertEqual(report['patterns']['conditional_deflection'], {'occurrences': 2, 'page_ids': ['guide']})
        self.assertEqual(report['benchmark']['status'], 'unavailable')

    def test_changed_source_rejects_old_reviews(self):
        p = page()
        with self.assertRaises(ValueError):
            assess_page(dict(p, content=HTML+' changed'), evaluated_at=AT, review=reviewed(p))

    def test_comparison_tracks_methodology_evaluators_and_missing_ids(self):
        p = page()
        r = reviewed(p)
        r['claims']['update'] = dict(span(p, '20 checks'), substantive_at='2026-11-01T00:00:00Z', change_evidence='fixture diff')
        before = assess_library([p], evaluated_at=AT, reviews={p['id']: r})
        after = copy.deepcopy(before)
        after['pages'][0]['score'] += 2
        delta = compare(before, after)
        self.assertEqual(delta['pages'][0]['score_delta'], 2)
        after['profile']['version'] = 'other'
        self.assertTrue(compare(before, after)['methodology_changed'])
        self.assertIsNone(compare(before, after)['pages'][0]['score_delta'])
        after = copy.deepcopy(before)
        after['pages'][0]['review']['passes'][0]['evaluator_version'] = 'new'
        self.assertTrue(compare(before, after)['pages'][0]['evaluator_changed'])
        self.assertIsNone(compare(before, after)['pages'][0]['score_delta'])


class ReportingTests(unittest.TestCase):
    def test_cross_format_roundtrip_and_xss(self):
        p = page('<h1>&lt;/script&gt;&lt;script&gt;window.BAD=1&lt;/script&gt;</h1><h2>Safe?</h2><p>Yes.</p>')
        report = assess_library([p], evaluated_at=AT)
        self.assertEqual(json.loads(to_json(report)), report)
        records = list(csv.DictReader(io.StringIO(to_csv(report))))
        reconstructed = json.loads(records[0]['payload_json'])
        reconstructed['pages'] = [json.loads(r['payload_json']) for r in records[1:]]
        self.assertEqual(reconstructed, report)
        md = to_markdown(report)
        self.assertIn(to_json(report), md)
        html = to_html(report)
        self.assertNotIn('</script><script>window.BAD', html)
        data = html.split('id="data">', 1)[1].split('</script>', 1)[0]
        self.assertEqual(json.loads(data), report)

    def test_bundle_and_cli_fresh_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            run = subprocess.run([sys.executable, '-m', 'seo_qa.botworthiness', 'demo', '--out', directory, '--at', AT], text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads((Path(directory)/'report.json').read_text())
            self.assertEqual(len(result['pages']), 2)
            self.assertTrue(result['coverage']['sampled'])
            for fmt in ['html', 'json', 'csv', 'md']:
                self.assertTrue((Path(directory)/('report.'+fmt)).stat().st_size)

    def test_refresh_retains_immutable_assessment_history(self):
        with tempfile.TemporaryDirectory() as directory:
            before = assess_library([page()], evaluated_at=AT)
            write_bundle(before, directory)
            write_bundle(before, directory)
            self.assertEqual(len(list((Path(directory)/'history').glob('*.json'))), 1)
            after = assess_library([page(HTML+'<p>New material.</p>')], evaluated_at=AT)
            write_bundle(after, directory)
            snapshots = [json.loads(p.read_text()) for p in (Path(directory)/'history').glob('*.json')]
            self.assertEqual(len(snapshots), 2)
            self.assertIn(before, snapshots)
            self.assertIn(after, snapshots)

    def test_cli_manifest_review_import_and_compare(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'article.html').write_text(HTML)
            (root/'input.json').write_text(json.dumps({'pages': [dict(id='guide', evidence_kind='synthetic', content_file='article.html')]}))
            def cli(command, *extra):
                run = subprocess.run([sys.executable, '-m', 'seo_qa.botworthiness', command, '--input', str(root/'input.json'), '--out', str(root/'out'), '--at', AT, *extra], text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr)
            cli('review-template')
            template = json.loads((root/'out/reviews.json').read_text())
            self.assertIsNone(template['guide']['claims'])
            (root/'reviews.json').write_text(json.dumps({'guide': reviewed(page())}))
            cli('assess', '--reviews', str(root/'reviews.json'))
            result = json.loads((root/'out/report.json').read_text())
            self.assertEqual(result['pages'][0]['criteria'][0]['points'], 24)
            run = subprocess.run([sys.executable, '-m', 'seo_qa.botworthiness', 'compare', '--input', str(root/'out/report.json'), '--after', str(root/'out/report.json'), '--out', str(root/'diff')], text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue((root/'diff/comparison.json').exists())

    def test_existing_scoring_and_acceptance_are_preserved(self):
        for outcomes, score in [(['pass'], 10), (['fail'], 1), (['pass', 'pass', 'fail'], 7), (['pass']*66+['fail']*34, 6.9), (['pass', 'inconclusive'], 5.5)]:
            findings = [dict(check=f'c-{i}', outcome=o, expected='expected') for i, o in enumerate(outcomes)]
            legacy = {'state': 'completed', 'acceptance_current': False, 'requirement': {'urls': ['test'], 'expected': {}}, 'observations': [{'url': 'test', 'findings': findings, 'evidence': {'sha256': 'a'*64}}]}
            saved = copy.deepcopy(legacy)
            legacy['scoring'] = score_report(legacy)
            library = assess_library([page()], evaluated_at=AT)
            augmented = attach_to_report(legacy, library)
            self.assertEqual(augmented['scoring']['score'], score)
            self.assertEqual(score_report(augmented), score_report(legacy))
            self.assertFalse(augmented['acceptance_current'])
            augmented.pop('quotability')
            self.assertEqual(augmented, legacy)
            legacy.pop('scoring')
            self.assertEqual(saved, legacy)


if __name__ == '__main__':
    unittest.main()
