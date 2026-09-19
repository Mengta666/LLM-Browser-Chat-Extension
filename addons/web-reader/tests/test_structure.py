import pytest
from lxml import etree

from app.reader import extract_html
from app.structure import serialize_body


@pytest.mark.parametrize('phrase', [
    'If <a href="/e"><code>KeyboardInterrupt</code></a> or <code>SystemExit</code> occurs, the initial exception is re-raised instead of a group.',
    'The <a href="/h"><code>Retry-After</code></a> header accepts seconds or an HTTP date, not only seconds.',
    '当 <a href="/p"><code>电源</code></a> 未断开时，<strong>不要</strong>进行校准；确认断电后才可以继续。',
])
def test_real_extractor_preserves_inline_order_and_qualifications(phrase):
    html = '<html><body><article><h2>Rules</h2><p>' + phrase + '</p><p>' + 'Background information for this technical reference. ' * 10 + '</p></article></body></html>'
    result = extract_html(html.encode(), 'https://example.org/', 10000)
    expected = ''.join(etree.fromstring('<p>' + phrase + '</p>').itertext())
    assert expected in result['content']
    assert 'href' not in result['content'] and 'https://example.org/e' not in result['content']
    matching = [b for b in result['blocks'] if expected in result['content'][b['start']:b['end']]]
    assert len(matching) == 1 and matching[0]['type'] == 'paragraph'


def test_lists_tables_and_code_are_atomic_and_offsets_are_exact():
    body = etree.fromstring('''<body><head rend="h2">Rules</head><p>Begin.</p>
      <list rend="ol"><item>First <hi>item</hi> ends.<list rend="ul"><item>Nested condition.</item></list></item><item>Second.</item></list>
      <code>if ready:\n    run()\n    stop()</code>
      <table><row><cell role="head">Name</cell><cell role="head">Time</cell></row><row><cell>Alpha</cell><cell>12 h</cell></row></table></body>''')
    result = serialize_body(body, 10000)
    values = [result['content'][b['start']:b['end']] for b in result['blocks']]
    assert [b['type'] for b in result['blocks']] == ['heading','paragraph','list','code','table']
    assert values[2] == '1. First item ends.\n   - Nested condition.\n2. Second.'
    assert values[3] == '```\nif ready:\n    run()\n    stop()\n```'
    assert values[4] == '| Name | Time |\n| --- | --- |\n| Alpha | 12 h |'
    assert '\n\n'.join(values) == result['content']
    assert all(b['section'] == 0 for b in result['blocks'])


def test_code_inside_list_preserves_lines_and_indentation():
    body = etree.fromstring('<body><list><item>Example:<code>if ready:\n    run()</code><p>Only after initialization.</p></item></list></body>')
    result = serialize_body(body,10000)
    assert result['content'] == '- Example:\n  ```\n  if ready:\n      run()\n  ```\n  Only after initialization.'


@pytest.mark.parametrize('tag', ['code','list','table'])
def test_oversized_atomic_block_not_cut(tag):
    value = {'code':'x' * 300, 'list':'<item>' + 'x' * 300 + '</item>',
             'table':'<row><cell>' + 'x' * 300 + '</cell></row>'}[tag]
    result = serialize_body(etree.fromstring('<body><p>Introduction.</p><' + tag + '>' + value + '</' + tag + '></body>'), 100)
    assert result['content'] == 'Introduction.' and result['truncated']


def test_block_count_bound_and_section_transitions():
    result = serialize_body(etree.fromstring('<body><head rend="h1">A</head><p>a</p><head rend="h2">B</head>' + '<p>b</p>' * 2200 + '</body>'), 100000)
    assert len(result['blocks']) == 2048 and result['truncated']
    assert [b['section'] for b in result['blocks'][:4]] == [0,0,2,2]
