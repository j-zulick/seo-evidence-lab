"""Labeling harness: template, validation, independence flag and agreement."""
import csv
import io

import pytest

from seo_qa import labeling
from seo_qa.evidence_search import load


def fill(sheet_text, answers, labeler='Synthetic Tester', kind='synthetic', own=None):
    rows = list(csv.DictReader(io.StringIO(sheet_text)))
    for row in rows:
        if row['query'] in answers:
            row['relevant'] = '1' if row['passage_id'] in answers[row['query']] else '0'
            row['labeler'], row['labeler_kind'] = labeler, kind
    if own:
        for row in rows:
            if row['query_id'] == 'own01':
                row['query'] = own[0]
                row['relevant'] = '1' if row['passage_id'] in own[1] else '0'
                row['labeler'], row['labeler_kind'] = labeler, kind
    out = io.StringIO()
    writer = csv.DictWriter(out, labeling.FIELDS, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def test_blank_sheet_has_every_query_passage_pair_and_no_labels():
    text = labeling.make_sheet()
    rows = list(csv.DictReader(io.StringIO(text)))
    passages = len(load()['passages'])
    assert len(rows) == (len(labeling.PROPOSED_QUERIES) + labeling.BLANK_QUERY_ROWS) * passages
    assert all(row['relevant'] == '' and row['labeler_kind'] == '' for row in rows)
    with pytest.raises(labeling.LabelError, match='no judgments'):
        labeling.parse_sheet(text)


def test_synthetic_labels_score_but_are_never_called_independent():
    text = fill(labeling.make_sheet(), {'stop crawlers indexing a page': {'robots'},
                                        'recipe for sourdough bread': set()},
                own=('preferred url', {'canonical'}))
    parsed = labeling.parse_sheet(text)
    assert set(parsed['queries']) == {'q07', 'q10', 'own01'}
    result = labeling.score([parsed])
    assert result['independent'] is False and 'NOT an independent' in result['limitations']
    cases = {c['query_id']: c for c in result['labelers'][0]['cases']}
    assert cases['q10']['baseline']['passed'] and cases['q10']['expanded']['passed']
    assert set(cases['q07']) >= {'baseline', 'expanded', 'outcome'}


def test_independent_flag_requires_every_sheet_to_declare_it():
    answers = {'interaction to next paint': {'speed'}}
    human = labeling.parse_sheet(fill(labeling.make_sheet(), answers, 'Reviewer A', 'human-independent'))
    author = labeling.parse_sheet(fill(labeling.make_sheet(), answers, 'Author', 'human-author'))
    assert labeling.score([human])['independent'] is True
    both = labeling.score([human, author])
    assert both['independent'] is False and both['agreement']['kappa'] in (1.0, None)


def test_agreement_detects_disagreement():
    a = labeling.parse_sheet(fill(labeling.make_sheet(), {'duplicate content signal': {'canonical'},
                                                          'button tap delay': {'speed'}}, 'A', 'human-independent'))
    b = labeling.parse_sheet(fill(labeling.make_sheet(), {'duplicate content signal': {'canonical'},
                                                          'button tap delay': set()}, 'B', 'human-independent'))
    agreement = labeling.score([a, b])['agreement']
    assert agreement['shared_judgments'] == 10 and agreement['observed_agreement'] == 0.9


@pytest.mark.parametrize('mutate,message', [
    (lambda r: r.update(relevant='yes'), '1 or 0'),
    (lambda r: r.update(labeler_kind='agent'), 'labeler_kind'),
    (lambda r: r.update(labeler=''), 'labeler name'),
    (lambda r: r.update(passage_id='ghost'), 'unknown passage'),
])
def test_invalid_rows_are_refused(mutate, message):
    text = fill(labeling.make_sheet(), {'duplicate content signal': {'canonical'}}, 'A', 'human-independent')
    rows = list(csv.DictReader(io.StringIO(text)))
    target = next(r for r in rows if r['query'] == 'duplicate content signal')
    mutate(target)
    out = io.StringIO()
    writer = csv.DictWriter(out, labeling.FIELDS, lineterminator='\n')
    writer.writeheader(); writer.writerows(rows)
    with pytest.raises(labeling.LabelError, match=message):
        labeling.parse_sheet(out.getvalue())


def test_partially_judged_query_is_refused():
    text = fill(labeling.make_sheet(), {'duplicate content signal': {'canonical'}}, 'A', 'human-independent')
    rows = list(csv.DictReader(io.StringIO(text)))
    next(r for r in rows if r['query'] == 'duplicate content signal')['relevant'] = ''
    out = io.StringIO()
    writer = csv.DictWriter(out, labeling.FIELDS, lineterminator='\n')
    writer.writeheader(); writer.writerows(rows)
    with pytest.raises(labeling.LabelError, match='judge every passage'):
        labeling.parse_sheet(out.getvalue())


def test_cli_writes_sheet_without_overwriting(tmp_path, capsys):
    path = tmp_path / 'sheet.csv'
    assert labeling.main(['sheet', '--out', str(path)]) == 0
    with pytest.raises(FileExistsError):
        labeling.main(['sheet', '--out', str(path)])
    filled = tmp_path / 'filled.csv'
    filled.write_text(fill(path.read_text(), {'interaction to next paint': {'speed'}}))
    assert labeling.main(['score', str(filled)]) == 0
    assert '"independent": false' in capsys.readouterr().out
