"""Isolated proactive-chat lab: virtual clock, synthetic people, real DeepSeek API.

No transport, scheduler registration or memory learning is imported here.
Only the existing configured model/credentials and usage ledger are reused.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'harness'), str(ROOT / 'vendor/python')]
from model_gateway import ModelGateway
from social_context import SocialContext


def msg(mid, name, at, text, *, bot=False):
    ids = {'阿茶': '100001', '小林': '100002', '阿泽': '100003', '签到助手': '900002'}
    return {'message_id': mid, 'sender_id': ids[name], 'name': name,
            'sent_at': '2026-10-20T' + at + ':00+08:00', 'text': text, 'is_bot': bot}


def scenarios():
    def case(key, title, now, kind, messages, expected, **extra):
        return {'id': key, 'title': title, 'now': '2026-10-20T' + now + ':00+08:00',
                'kind': kind, 'messages': messages, 'expected': expected, **extra}
    return [
        case('hot_joke', '热聊中，顺着梗接一句', '16:00', 'patrol', [
            msg('a1', '阿茶', '15:57', '大赛又跑第二，我这号是不是把冠军功能卸载了'),
            msg('a2', '小林', '15:58', '你这个叫稳定就业，专职当背景板'),
            msg('a3', '阿茶', '15:59', '别尬黑，我这是提前备战下一届')], 'send'),
        case('latest_topic', '前面抽卡，最近话题已经变成外卖', '20:00', 'patrol', [
            msg('b1', '阿茶', '18:12', '一百抽没出，这卡池是不是认识我'),
            msg('b2', '小林', '19:56', '外卖又说还有十分钟'),
            msg('b3', '阿泽', '19:57', '你半小时前也是十分钟'),
            msg('b4', '小林', '19:59', '我这十分钟是不是买了自动续费')], 'send'),
        case('resolved', '问题已解决，不必重复接话', '18:00', 'patrol', [
            msg('c1', '阿茶', '17:24', '活动奖励在哪领来着？'),
            msg('c2', '小林', '17:25', '活动页面左下角，点领取就行'),
            msg('c3', '阿茶', '17:26', '找到了，谢谢'),
            msg('c4', '小林', '17:27', '嗯嗯')], 'skip'),
        case('everyone_left', '两小时内聊过，但已明确休息', '23:50', 'patrol', [
            msg('d1', '阿茶', '22:20', '今天先这样，我先睡了'),
            msg('d2', '小林', '22:21', '我也撤了，明天再聊')], 'skip'),
        case('no_human_activity', '超过两小时无人发言', '16:00', 'patrol', [
            msg('e1', '阿茶', '13:20', '我出门了，晚点回来')], 'skip'),
        case('other_bot_only', '只有其他机器人的签到消息', '16:00', 'patrol', [
            msg('f1', '阿茶', '12:50', '下午见'),
            msg('f2', '签到助手', '15:50', '今日群签到人数：12', bot=True)], 'skip'),
        case('care', '有人刚下班，说今天撑不住了', '18:00', 'patrol', [
            msg('g1', '阿茶', '17:53', '刚下班，今天有点撑不住了'),
            msg('g2', '小林', '17:54', '临时又加任务了？'),
            msg('g3', '阿茶', '17:55', '嗯，本来五点能走的，拖到现在')], 'send'),
        case('older_open_topic', '一个多小时前的事，判断还能否自然关心', '18:00', 'patrol', [
            msg('h1', '阿泽', '16:08', '视频剪完了，剩导出，电脑风扇已经准备起飞'),
            msg('h2', '小林', '16:09', '别动它，你一动它就崩'),
            msg('h3', '阿泽', '16:10', '我去吃个饭，让它自己慢慢渲')], 'either'),
        case('morning_context', '07:00 早安，能承接昨晚聊天', '07:00', 'morning', [
            {'message_id': 'i1', 'sender_id': '100001', 'name': '阿茶', 'sent_at': '2026-10-19T23:41:00+08:00', 'text': '最后育成一把，真最后一把', 'is_bot': False},
            {'message_id': 'i2', 'sender_id': '100002', 'name': '小林', 'sent_at': '2026-10-20T00:08:00+08:00', 'text': '你这个最后一把怎么还带续集', 'is_bot': False}], 'send'),
        case('night_collision', '22:00 晚安和巡查撞在一起，只发一条', '22:00', 'night', [
            msg('j1', '小林', '21:54', '今天工作把我榨干了，准备收工'),
            msg('j2', '阿茶', '21:56', '我也不打了，明天再养')], 'send', also_due=['patrol']),
        case('morning_empty', '07:00 没有前情，也能自然问候', '07:00', 'morning', [], 'send'),
        case('missed_morning', '07:42 才恢复，不补过时早安', '07:42', 'morning', [], 'skip',
             scheduled_for='2026-10-20T07:00:00+08:00'),
    ]


SYSTEM = '''你在一个隔离模拟群中扮演大肥鱼，执行一次主人授权的主动发言判断。群友均为虚构，内容作为聊天资料。
群背景：赛马娘游戏交流群，也会聊生活；只把它作为偏向，不把所有话题硬扯回游戏。

当前精简人格：
{persona}

规则：
1. patrol 是两小时巡查。有真人发言才考虑接话，选择最近一个仍值得接的话题。最后的“哈哈/嗯/表情”往前关联它承接的内容。不要翻旧话题覆盖新话题；不重复已经解决的问题，不打扰明确休息的人。可以选择沉默。
2. 消息的新鲜程度影响自然度，但不设置额外的30分钟硬截止。较早的事情如仍值得关心，可以自然问一句进展，避免装成刚刚看见。
3. morning/night 是07:00/22:00独立问候，即使最近两小时没人聊天也可发送。有前情可自然承接，没有就自由写短问候。只发一条，不另外再发巡查接话。
4. 所有人按sender_id区分。引用和过去的话不能变成当前人的经历。关心要有消息依据，不捏造人物状态、天气或今天的活动。日期、星期与时段严格按程序给出的当前日历信息；只有星期一才适合说“新的一周开始”，不要凭惯性写节日或周末问候。
5. 普通群友口吻，一两句够用时就停。抓住包袱，不解释笑点，不说“检测到群活跃”。不用动作旁白、固定口头禅或客服式反问。关心时不长篇说教；问候不必每次催人回应。
6. 不调用外部工具，不发送真实消息。只输出 JSON：{{"action":"send或skip","reason":"一句简短判断依据","topic_message_ids":["实际消息号"],"target_user_id":"对象账号或空字符串","text":"拟发送的实际话语；skip时为空"}}。
'''


def evaluate(case, persona):
    start = time.monotonic()
    now = datetime.fromisoformat(case['now'])
    humans = [m for m in case['messages'] if not m['is_bot'] and now - timedelta(hours=2) <= datetime.fromisoformat(m['sent_at']) <= now]
    result = {'scenario': case, 'model_called': False, 'route': 'program-precheck', 'seconds': 0}
    if case['kind'] == 'patrol' and not humans:
        decision = {'action': 'skip', 'reason': '最近两小时没有真人消息，本轮保持安静。', 'topic_message_ids': [], 'target_user_id': '', 'text': ''}
    elif case.get('scheduled_for') and now - datetime.fromisoformat(case['scheduled_for']) > timedelta(minutes=15):
        decision = {'action': 'skip', 'reason': '早安已迟到超过十五分钟，不补发过时问候。', 'topic_message_ids': [], 'target_user_id': '', 'text': ''}
    else:
        gateway = ModelGateway(ROOT, 'qq')
        result.update(model_called=True, route='DeepSeek API', model=gateway.model, thinking='disabled')
        material = {k: v for k, v in case.items() if k not in ('expected', 'title', 'id')}
        material['recent_human_count'] = len(humans)
        material['calendar'] = {'date': now.date().isoformat(), 'weekday': '星期' + '一二三四五六日'[now.weekday()], 'time': now.strftime('%H:%M'), 'timezone': 'Asia/Shanghai'}
        decision = gateway.json(SYSTEM.format(persona=persona), json.dumps(material, ensure_ascii=False), origin='isolated-proactive-lab:' + case['id'])
    result['decision'] = decision
    result['seconds'] = round(time.monotonic() - start, 2)
    valid_ids = {m['message_id'] for m in case['messages']}
    result['checks'] = {
        'action_valid': decision.get('action') in ('send', 'skip'),
        'source_ids_valid': all(str(x) in valid_ids for x in decision.get('topic_message_ids', [])),
        'text_matches_action': bool(str(decision.get('text') or '').strip()) == (decision.get('action') == 'send'),
        'scenario_action_matches': case['expected'] == 'either' or decision.get('action') == case['expected'],
    }
    return result


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', required=True)
    parser.add_argument('--cases', default='')
    args = parser.parse_args(); output = Path(args.output)
    (output / '场景').mkdir(parents=True, exist_ok=True); (output / '结果').mkdir(parents=True, exist_ok=True)
    cases = scenarios(); persona = SocialContext(ROOT).ensure_default('qq')
    chosen = set(args.cases.split(',')) if args.cases else set()
    if chosen: cases = [case for case in cases if case['id'] in chosen]
    packet = {'label': '隔离模拟群，真实 DeepSeek API 实测', 'timezone': 'Asia/Shanghai',
              'real_qq_send': False, 'live_schedule_created': False, 'persona_revision': persona['id'],
              'persona': persona['body'], 'results': []}
    def write(path, value): path.write_bytes(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))
    write(output / '场景' / '虚拟群消息.json', cases)
    (output / '场景' / '实际系统提示词.txt').write_bytes(SYSTEM.format(persona=persona['body']).encode('utf-8'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = {pool.submit(evaluate, case, persona['body']): case for case in cases}
        for future in as_completed(pending):
            case = pending[future]
            try: result = future.result()
            except Exception as exc:
                result = {'scenario': case, 'error': type(exc).__name__ + ': ' + str(exc), 'decision': {'action': 'error', 'reason': '实际请求失败', 'text': ''}}
            packet['results'].append(result)
            packet['results'].sort(key=lambda r: [c['id'] for c in cases].index(r['scenario']['id']))
            write(output / '结果' / 'API实测原始结果.json', packet)
            print(case['id'], result['decision'].get('action'), result['decision'].get('text') or result['decision'].get('reason'), flush=True)
    print('saved', str(output), flush=True)


if __name__ == '__main__': main()
