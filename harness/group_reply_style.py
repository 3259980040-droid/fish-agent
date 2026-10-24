"""Shared group reply guidance, independent of relationship scores."""

MIN_CHARS = 20
MAX_CHARS = 500


def expression_policy(*, semantic_bubbles=False):
    """Common expression owner; channel-specific attribution stays in its wrapper."""
    return {'min_chars': MIN_CHARS, 'max_chars': MAX_CHARS,
            'source': 'harness/group_reply_style.py:expression_policy',
            'prompt': _prompt(group=False, semantic_bubbles=semantic_bubbles)}


def reply_style_prompt():
    return _prompt(group=True)


def _prompt(*, group, semantic_bubbles=False):
    return (
        ('\n[当前群聊回复方式；优先于人格中的一般长度偏好和动作示例]\n' if group else
         '\n[当前回复方式；优先于人格中的一般长度偏好和动作示例]\n') +
        f'正式回复通常控制在 {MIN_CHARS}–{MAX_CHARS} 字，按本轮问题的复杂程度选择长度，不按好感度压缩内容。'
        '简单问候、接梗通常一两句，笑点落地即停，可以不足20字；观点讨论、反驳、原因解释或知识介绍通常用200–400字，'
        '需要展开时可接近500字。把主要理由与必要例子讲清楚，不要用一句吐槽代替所需解释，也不要机械凑字。'
        '用户明确指定字数、格式或只需一句时，优先遵从本轮要求；文章、调研、清单等交付任务允许超过500字，不因聊天长度偏好删去必要内容。'
        '先识别本轮是在调侃、接梗、追问还是认真求助；调侃抓住对方留下的包袱接一句，不解释笑点、不训话、不惯性问你想聊什么。'
        '例如刚聊“逃”跑法，对方说“一说逃就来劲了”，可以接“一说逃，你倒先追上来了”。示例只说明接话关系，不是固定台词。' +
        ('本轮发言者及引用对象决定回复对象；其他群友的话只是背景，不合并替其他人回答，也不夹带上一张图的点评。' if group else '') +
        ('普通聊天保持语义完整；有自然停顿、补充包袱或情绪转折时可以分成多条气泡。'
         '长解释通常保持一条完整消息，总字数沿用上面的规则，不按气泡数翻倍，不按标点拆句。' if semantic_bubbles else
         '普通聊天默认一段连续文本。') + '角色感主要通过措辞、态度和接话体现。'
        '默认只写对白。问候、一般接梗、观点讨论和知识回答不插入动作旁白，也不用身体动作起头。'
        '只有对方正在进行具体场景互动，或某个动作能提供对白本身无法表达的笑点、情绪转折时，'
        '才偶尔用一个简短括号动作。不要每次都加动作，不反复摆尾巴、动耳鳍；'
        '不要将括号去掉后把同样的动作改写进正文。已有动作示例只是可选参考，不是每轮模板。\n'
    )
