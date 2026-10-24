"""Summarize actual replay evidence. Does not create model calls or touch live data."""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import sqlite3
from contextlib import closing

NAMES={'P':'主人私聊','A':'群A','B':'群B'}
TARGETS={'P':'private:10001001','A':'group:20002001','B':'group:20002002'}


def read(path):
    return json.loads(path.read_text('utf-8'))


def text_of(send):
    return '\n'.join(s.get('text','') for s in send['args'].get('segments',[]) if s.get('type')=='text')


def system_send(send):
    return '系统故障通知' in text_of(send) or 'fault-notice' in str(send['args'].get('idempotency_key',''))


def bubble_text(decision):
    raw=decision.get('output')
    if raw:
        try:
            value=json.loads(raw)
            return '\n'.join(str(b.get('text') or '[表情]') for b in value.get('bubbles',[]))
        except (ValueError,TypeError): return str(raw)
    return str(decision.get('error') or '无正文记录')


def delay_label(values):
    if not values:return '未进入'
    return f'{statistics.median(values)/1000:.1f}s（{min(values)/1000:.1f}–{max(values)/1000:.1f}）'


def collect(base):
    results=[(p,read(p)) for p in sorted(base.glob('M*-r*/结果.json'))]
    by_mode={f'M{i}':[(p,d) for p,d in results if d['mode']==f'M{i}'] for i in range(6)}
    summary={}
    for mode,runs in by_mode.items():
        delay={p:[] for p in NAMES}
        first=Counter(); sent=not_sent=failed=main=opportunities=0
        wrong_quotes=[]; cross=[]; texts=[]; statuses=Counter(); tool_counts=Counter(); quote_count=0
        by_place={p:Counter() for p in NAMES}; tool_failures=[]; image_results=[]
        for path,d in runs:
            for epoch in range(3):
                subset=[p for p in d.get('processed',[]) if p['epoch']==epoch]
                if subset:first[subset[0]['place']]+=1
            for step in d.get('processed',[]):
                opportunities+=1
                epoch,place=step['epoch'],step['place']
                by_place[place]['opportunities']+=1
                traces=[t for t in d.get('model_trace',[]) if t['epoch']==epoch and t['place']==place]
                if traces:
                    delay[place].append(min(t.get('request_wait_ms',0) for t in traces))
                    by_place[place]['entered_model']+=1
                decisions=[v for v in d.get('decisions',[]) if v['epoch']==epoch and v['place']==place]
                main+=bool(decisions)
                by_place[place]['main_turns']+=bool(decisions)
                for decision in decisions:
                    texts.append({'mode':mode,'repeat':d['repeat'],'epoch':epoch,'place':place,
                                  'text':bubble_text(decision),'path':str(path)})
                prefix=f'{mode}-r{d["repeat"]}-e{epoch}-{place}'
                sends=[s for s in d.get('fake_channel',[]) if s.get('turn')==prefix and not system_send(s)]
                errors=[v['error'] for v in decisions if v.get('error') and not v['error'].startswith('TurnSilent')]
                errors += [t['error'] for t in traces if t.get('error')]
                if errors:
                    failed+=1;statuses['失败']+=1
                    by_place[place]['failed']+=1
                elif sends:
                    sent+=1;statuses['已交假通道']+=1
                    by_place[place]['sent']+=1
                else:
                    not_sent+=1
                    statuses['已结束未发送' if decisions else '未进入主生成']+=1
                    by_place[place]['not_sent']+=1
                for s in sends:
                    target=s['args'].get('chat_id')
                    if target!=TARGETS[place]:cross.append({'repeat':d['repeat'],'epoch':epoch,'from':place,'to':target})
                    quote=str(s['args'].get('reply_to') or '')
                    if quote:
                        quote_count+=1
                        prefix_expected={'private:10001001':'700001','group:20002001':'700002','group:20002002':'700003'}.get(target)
                        if prefix_expected and not quote.startswith(prefix_expected):
                            wrong_quotes.append({'target':target,'quote':quote})
            tool_counts.update(t['name'] for t in d.get('tools',[]))
            for tool in d.get('tools',[]):
                if tool.get('error') or tool.get('result',{}).get('ok') is False:
                    tool_failures.append({'repeat':d['repeat'],**tool})
                if tool['name']=='fish_image_look':
                    image_results.append({'repeat':d['repeat'],**tool})
        ledgers=[row for _,d in runs for row in d.get('ledger_rows',[])]
        reranks=[x for _,d in runs for x in d.get('memory_reranks',[])]
        summary[mode]={'runs':len(runs),'repeats':[d['repeat'] for _,d in runs],
            'fatal_errors':[d.get('fatal_error') for _,d in runs if d.get('fatal_error')],
            'opportunities':opportunities,'main_turns':main,'sent_turns':sent,'not_sent_turns':not_sent,'failed_turns':failed,
            'first_admitted':dict(first),'delay':{p:delay_label(v) for p,v in delay.items()},
            'by_place':{p:dict(v) for p,v in by_place.items()},
            'max_unconsidered':{p:max((d.get('max_unconsidered',{}).get(p,0) for _,d in runs),default=0) for p in NAMES},
            'quote_errors':wrong_quotes,'quote_count':quote_count,'cross_target_sends':cross,'statuses':dict(statuses),
            'tools':dict(tool_counts),'memory_rerank_calls':len(reranks),
            'memory_order_changed':sum(x['changed'] for x in reranks),
            'requests':sum(d.get('requests_delta',0) for _,d in runs),
            'request_statuses':dict(Counter(x['status'] for x in ledgers)),
            'input_tokens':sum(x['input_tokens'] for x in ledgers),
            'output_tokens':sum(x['output_tokens'] for x in ledgers),
            'guard_blocks':[x for _,d in runs for x in d.get('guard',{}).get('blocked',[])],
            'all_checked_harness_unchanged':all(all(d.get('harness_unchanged',{}).values()) for _,d in runs),
            'tool_failures':tool_failures,'image_results':image_results,
            'texts':texts,'paths':[str(p) for p,_ in runs]}
    return summary,by_mode


def render(base,summary,by_mode):
    lines=['# 多场所对照实验：实际结果','',
           '日期：2026-10-24。真实 DeepSeek API、真实 Fish 生成/工具裁决/群回合，QQ 仅假通道。',
           '此文件由实际结果汇总；分析结论及请裁决见上级目录《结果.md》。','',
           '## M0–M5 × 六项观测','',
           '延迟为受控批次到首个模型请求，含串行等待及真实 API 时间；不是生产端到端时延。',
           '“首入场”是实验调度顺序，不是模型主动选目标。未进入请求的机会不以 0 秒填充。','',
           '| 方式 | ①到模型延迟中位数/范围：私聊；A；B | ②首入场 P/A/B | ③已交假通道/未发/执行失败（回合） | ④连续未考虑最大 P/A/B | ⑤引用校验 | ⑥状态记录 |',
           '|---|---|---|---|---|---|---|']
    for mode,d in summary.items():
        first='/'.join(str(d['first_admitted'].get(p,0)) for p in NAMES)
        streak='/'.join(str(d['max_unconsidered'].get(p,0)) for p in NAMES)
        delay='；'.join(d['delay'][p] for p in NAMES)
        lines.append(f'| {mode}（{d["runs"]}次） | {delay} | {first} | {d["sent_turns"]}/{d["not_sent_turns"]}/{d["failed_turns"]} | {streak} | 有引用 {d["quote_count"]}，跨场所引用错误 {len(d["quote_errors"])}；语义归属另人工判读 | {json.dumps(d["statuses"],ensure_ascii=False)}；进行中状态未验证 |')
    lines+=['','M4 只有主人私聊驱动的 9 个机会，其他方式最多 27 个；因此它的总量与时延不用于证明私聊偏重。',
            '连续未考虑值反映这段有限回放；每个批次都会遍历待处理场所，不能由其低值证明长期防饥饿。','',
            '首个模型请求包含群预筛请求，不能当成已进入主回复生成。表中失败为本回合生成/交付的终止错误；中途工具失败另计。',
            '假通道只返回成功回执，没有注入真实 QQ 断连或 uncertain；因此发送失败为 0 不证明发送链健壮性。',
            '入场等待由实验驱动记录；检索工具结果可事后查看，但本轮没有证明生产界面能实时显示“正在检索”。','',
            '## 真实调用与用量','',
            '| 方式 | 账本请求增量 | 输入 token | 输出 token | 状态 | 重排发生/调用 |',
            '|---|---:|---:|---:|---|---|']
    for mode,d in summary.items():
        lines.append(f'| {mode} | {d["requests"]} | {d["input_tokens"]} | {d["output_tokens"]} | {json.dumps(d["request_statuses"],ensure_ascii=False)} | {d["memory_order_changed"]}/{d["memory_rerank_calls"]} |')
    lines+=['','token 为服务商回传后入账的数值；未返回计量不估算。tool_events 是工具功能计数，不与请求数相加；DeepSeek 模式的视觉请求已在 requests 中。没有用价目表推算金额。','',
            '## 每种方式两条典型原文','']
    for mode,runs in by_mode.items():
        lines+=['### '+mode,'']
        candidates=[(path,d,v) for path,d in runs for v in d.get('decisions',[])]
        samples=[]
        preferred={'M0':[('A',2),('B',2)],'M1':[('P',1),('P',2)],
                   'M2':[('P',0),('A',2)],'M3':[('P',1),('A',2)],
                   'M5':[('P',1),('A',2)]}.get(mode,[])
        for place,epoch in preferred:
            match=next((x for x in candidates if x[2]['place']==place and x[2]['epoch']==epoch),None)
            if match:samples.append(match)
        if mode=='M4':
            dual=[x for x in candidates if x[2]['epoch']==2]
            samples=[dual[0],dual[-1]] if len(dual)>1 else dual
        for item in candidates:
            if len(samples)>=2:break
            if item not in samples:samples.append(item)
        for number,(path,d,v) in enumerate(samples[:2],1):
            step=next((s for s in d.get('processed',[]) if s['epoch']==v['epoch'] and s['place']==v['place']),{})
            current='\n'.join(str(b.get('text','[多模态输入]')) for b in v['current'])
            lines += [f'**样本{number}：第{d["repeat"]}次，第{v["epoch"]+1}批，{NAMES[v["place"]]}，触发 {v["trigger"]}**','',
                      f'本批入场序号：{step.get("order",-1)+1}；这是调度记录，不是模型选择。','',
                      '她看到的当前输入原文（最多 1400 字，完整请求在本次 RequestInspector 副本中）：','',
                      '```text',current[:1400],'```','',
                      f'历史来源：`{v["history_source"]}`；历史项 {v["history_count"]}；人格摘要 `{v["persona_body_sha256"][:12]}`。','',
                      '实际返回/未发送状态：','','```text',bubble_text(v),'```','']
            if mode=='M4':
                marker='[实验同语料多场所概况，不是必须发言指令]'
                shared=v.get('references','').partition(marker)[2]
                lines+=['本轮额外提供的多场所概况原文：','','```text',shared.strip(),'```','']
            turn=f'{mode}-r{d["repeat"]}-e{v["epoch"]}-{v["place"]}'
            sends=[s for s in d.get('fake_channel',[]) if s.get('turn')==turn and not system_send(s)]
            lines+=['实际假通道回执：'+('无' if not sends else ''),'']
            for send in sends:
                lines+=['- `'+send['args'].get('chat_id','')+'`：'+text_of(send)+'；回执 `'+send['message_id']+'`。']
            lines+=['']
            reasons=[]
            # Reasons must belong to this decision, not another epoch at the
            # same place. The captured model trace has exact decision bounds.
            for request in d.get('model_trace',[])[v['model_trace_from']:v['model_trace_to']]:
                for call in request.get('tools',[]):
                    if call['name'] != 'fish_say':
                        continue
                    arguments=call.get('arguments',{})
                    if isinstance(arguments,str):
                        try: arguments=json.loads(arguments)
                        except ValueError: continue
                    if arguments.get('reason'):
                        reasons.append(arguments['reason'])
            lines+=['为什么：'+('发送工具提交的原 reason：'+'；'.join(dict.fromkeys(reasons)) if reasons else
                                 '该样本未提供可直接引用的发送 reason；不补写模型心理动机。'),'',
                    '证据：['+path.parent.name+'](<'+path.as_posix()+'>)。','']
    lines+=['## 未证明的部分','',
            '- 每种方式 3 次，小样本且使用真实模型随机输出，没有显著性或提升百分比结论。',
            '- M0 是批次化、串行受控回放，复用原入口与规则；不是在线并发生产基线。',
            '- M5 在现状已分场所组装的路径上重复，没有凭空制造第二套人格/记忆。',
            '- M2 是实验脚本内的加权随机与服务账；未覆盖持续高负载、长任务和新场所激活的长期公平性。',
            '- 当前语料的纯图是否进入视觉模型要看真实请求/工具回执；不能把图片传递失败归为视觉模型能力弱。',
            '- 引用目标机械校验不等于自然语言中人物、代词和关系全部正确；需结合原文判读。',
            '- 没有制作客户端界面，后台可解释事件不等于 UI 已显示这些状态。',
            '- 临时根初始化映射账号、合成第二群、清空旧运行身份；所有正式方式条件相同，原样阻断先导另存。','']
    return '\n'.join(lines)


def account_all(base):
    """Include interrupted attempts; never infer charges from live counters."""
    artifacts=Path(__file__).resolve().parents[1]/'artifacts'/'multi-place-20261024'
    def rows(root,table):
        path=root/'data/qq/model-usage.db'
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM '+table)]
    baseline=artifacts/'frozen-source'
    old={table:{r['id'] for r in rows(baseline,table)} for table in ('requests','tool_events')}
    final={p.parent.name for p in base.glob('M*-r*/结果.json')}
    details=[]
    for root in sorted(artifacts.glob('M*-r*/root')):
        added={table:[r for r in rows(root,table) if r['id'] not in old[table]] for table in old}
        req=added['requests']
        details.append({'run':root.parent.name,'category':'最终对照' if root.parent.name in final else '先导或中止',
                        'requests':len(req),'statuses':dict(Counter(r['status'] for r in req)),
                        'input_tokens_recorded':sum(r['input_tokens'] or 0 for r in req),
                        'output_tokens_recorded':sum(r['output_tokens'] or 0 for r in req),
                        'request_rows':req,'tool_events':added['tool_events']})
    totals={}
    for category in ('最终对照','先导或中止'):
        selected=[r for r in details if r['category']==category]
        totals[category]={'runs':len(selected),'requests':sum(r['requests'] for r in selected),
            'input_tokens_recorded':sum(r['input_tokens_recorded'] for r in selected),
            'output_tokens_recorded':sum(r['output_tokens_recorded'] for r in selected),
            'statuses':dict(sum((Counter(r['statuses']) for r in selected),Counter())),
            'tool_events':sum(len(r['tool_events']) for r in selected)}
    document={'baseline':str(baseline),'totals':totals,'details':details,
              'note':'按副本新增请求ID归因；中止前未回传用量不能估算，不据此声称零付费。'}
    destination=base.parent
    (destination/'完整用量.json').write_text(json.dumps(document,ensure_ascii=False,indent=2),'utf-8')
    lines=['# 本轮完整模型用量（包含先导与中止）','',
        '主人已明确授权真实模型调用。按各临时根新增请求 ID 归因；没有以生产在线计数的变化冒充实验用量。','',
        '| 阶段 | 临时根数 | 账本请求 | 已记输入 token | 已记输出 token | 请求状态 | 工具事件 |',
        '|---|---:|---:|---:|---:|---|---:|']
    for name,r in totals.items():
        lines.append(f'| {name} | {r["runs"]} | {r["requests"]} | {r["input_tokens_recorded"]} | {r["output_tokens_recorded"]} | {json.dumps(r["statuses"],ensure_ascii=False)} | {r["tool_events"]} |')
    lines+=['','上述 token 仅为已回传并入账的数量；被中止的 pending 请求可能已到服务商，不能断言未收费，也不能估算缺失 token。',
            'requests 是账本记录数，不等于全部都已到服务商。工具事件单列，不与请求数相加；DeepSeek 模式的视觉请求已计入 requests。缓存命中不算额外付费视觉调用。',
            '无价格、折扣和账户扣款回执，故不推算金额。先导/中止结果没有混入最终对照指标。','',
            '逐临时根明细见同目录《完整用量.json》。','']
    (destination/'完整用量.md').write_text('\n'.join(lines),'utf-8')
    return totals


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',type=Path,required=True)
    p.add_argument('--account-all',action='store_true');args=p.parse_args()
    summary,by_mode=collect(args.base)
    (args.base/'汇总.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),'utf-8')
    (args.base/'对照表与典型原文.md').write_text(render(args.base,summary,by_mode),'utf-8')
    if args.account_all:account_all(args.base)
    print(json.dumps({m:{k:v for k,v in d.items() if k in ('runs','requests','sent_turns','not_sent_turns','failed_turns','memory_order_changed','fatal_errors')}
                      for m,d in summary.items()},ensure_ascii=False))


if __name__=='__main__':main()
