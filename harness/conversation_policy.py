"""Persistent community context shared by normal and test group routes."""
from locations import project_path
from storage import read_json, write_json


def community(root, chat):
    return read_json(project_path(root, 'harness/conversations.json'), {}).get('groups', {}).get(str(chat).removeprefix('group:'), {})


def set_community(root, chat, topic):
    if not isinstance(topic, str) or len(topic) > 1000:
        raise ValueError('群背景需为1000字以内的文字')
    path = project_path(root, 'harness/conversations.json')
    cfg = read_json(path, {})
    cfg.setdefault('groups', {}).setdefault(str(chat).removeprefix('group:'), {})['topic'] = topic.strip()
    write_json(path, cfg)
    return community(root, chat)


def community_prompt(root, chat, *, has_image=False):
    topic = community(root, chat).get('topic', '')
    if not topic:
        return ''
    text = '\n[本群背景，主人设置]\n' + topic + '\n群背景只是候选偏向，不能据此认定所有图片都属于本群游戏。'
    if has_image:
        text += ('识图时先核对可见发型与挑染位置、瞳孔、头饰、服装纹样等特征；优先在群主题相关角色中寻找候选，'
                 '视觉特征和对方已明确的纠正优先于群主题；存在冲突时保留其他作品候选。'
                 '把观察、角色名候选与已核对身份分开；特征相似不能直接断定身份。'
                 '不展示内部思考过程；只给出判断、最有辨识度的一两处依据及必要的不确定性。')
    return text + '\n'
