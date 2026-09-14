import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent.context_builder import _format_element, _group_and_format_elements, build_observation_message
from agent.state import PageState


def test_empty_link_keeps_hint_role_and_position():
    line = _format_element({'id': 12, 'tag': 'a', 'role': 'link', 'text': '',
                            'target_hint': 'plugin.php; operation=qiandao',
                            'bounding_box': {'x': 30, 'y': 40, 'width': 180, 'height': 65}})
    assert all(text in line for text in ('[12]', 'role=link', '无名称', 'operation=qiandao', 'bbox=(30,40,180,65)'))


def test_grouping_cannot_erase_position_or_duplicate_identity():
    elements = [{'id': i, 'tag': 'a', 'text': '同名',
                 'bounding_box': {'x': i * 10, 'y': 40, 'width': 10, 'height': 10}} for i in range(3)]
    lines = _group_and_format_elements(elements)
    assert len(lines) == 3
    assert all(f'[{i}]' in lines[i] and 'bbox=' in lines[i] for i in range(3))


def test_screenshot_status_survives_protocol_and_matches_prompt():
    state = PageState(screenshot='data:image/jpeg;base64,synthetic', screenshot_marked=True, screenshot_mark_ids=[12, 18])
    assert '[12,18]' in build_observation_message(state)
    state.screenshot_marked = False
    assert '未添加编号' in build_observation_message(state)
    state.screenshot = ''
    assert '截图不可用' in build_observation_message(state)
