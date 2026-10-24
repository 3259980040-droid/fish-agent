"""Render unedited API test replies as clearly labelled simulated-chat images."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'vendor/python')]
from PIL import Image, ImageDraw, ImageFont

W = 1120
FONT = Path('C:/Windows/Fonts/msyh.ttc')
BOLD = Path('C:/Windows/Fonts/msyhbd.ttc')


def font(size, bold=False):
    return ImageFont.truetype(str(BOLD if bold else FONT), size)


class Page:
    def __init__(self, title, number):
        self.image = Image.new('RGB', (W, 6500), '#f3f5f8')
        self.d = ImageDraw.Draw(self.image)
        self.y = 0; self.number = number
        self.d.rectangle((0, 0, W, 172), fill='#17263c')
        self.d.text((42, 24), '大肥鱼 · 主动水群模拟实测', font=font(32, True), fill='white')
        self.d.text((42, 77), title, font=font(24), fill='#d5e1f5')
        self.d.text((42, 124), '构造群聊场景 / DeepSeek API 实际输出 / 未发送到真实 QQ 群', font=font(18), fill='#afc1dc')
        self.y = 200

    def lines(self, text, face, width):
        result=[]
        for paragraph in str(text).split('\n'):
            line=''
            for char in paragraph:
                if line and self.d.textlength(line+char,font=face)>width:
                    result.append(line);line=char
                else:line+=char
            result.append(line)
        return result

    def text(self, text, x=42, face=None, color='#25344b', width=1036, gap=9):
        face=face or font(23)
        for line in self.lines(text,face,width):
            self.d.text((x,self.y),line,font=face,fill=color)
            self.y+=face.size+gap

    def section(self, title, subtitle=''):
        if self.y>210:
            self.y+=12
            self.d.line((42,self.y,1078,self.y),fill='#dbe2ec',width=2)
            self.y+=28
        self.text(title,face=font(27,True),color='#17263c')
        if subtitle:self.text(subtitle,face=font(20),color='#728098')
        self.y+=12

    def message(self, name, text, at='', model=False, warning=False):
        face=font(25)
        lines=self.lines(text,face,810)
        bw=max(190,min(850,max(self.d.textlength(line,font=face) for line in lines)+40))
        bh=34*len(lines)+34
        bx=100 if model else W-100-bw
        avatar_x=48 if model else W-70
        self.d.ellipse((avatar_x,self.y+28,avatar_x+42,self.y+70),fill='#5078ba' if model else '#8b9aaf')
        initial='鱼' if model else name[-1:]
        self.d.text((avatar_x+8,self.y+33),initial,font=font(22,True),fill='white')
        label=(name+' · API 实际回复') if model else name+' · 模拟群友'
        self.d.text((bx,self.y),label+'  '+at,font=font(18),fill='#586982')
        top=self.y+30
        fill='#fff1ef' if warning else '#ffffff' if model else '#dbeafa'
        outline='#f0b4a8' if warning else '#dce4ef'
        self.d.rounded_rectangle((bx,top,bx+bw,top+bh),radius=17,fill=fill,outline=outline,width=1)
        for i,line in enumerate(lines):self.d.text((bx+20,top+15+i*34),line,font=face,fill='#243248')
        self.y=top+bh+21

    def note(self, text, positive=True):
        face=font(20);lines=self.lines(text,face,986);height=len(lines)*29+28
        self.d.rounded_rectangle((42,self.y,1078,self.y+height),radius=12,
                                 fill='#e5f1ed' if positive else '#f5eadb')
        for i,line in enumerate(lines):self.d.text((62,self.y+12+29*i),line,font=face,fill='#346856' if positive else '#835e29')
        self.y+=height+21

    def scenario(self, row, *, title=None, show_messages=True):
        case=row['scenario'];decision=row['decision']
        kind={'patrol':'两小时巡查','morning':'早安触发','night':'晚安触发'}[case['kind']]
        self.section(title or case['title'],kind+' · 虚拟时间 '+case['now'][11:16]+'（北京时间）')
        if show_messages:
            for m in case['messages']:
                self.message(m['name'],m['text'],m['sent_at'][11:16])
        if decision['action']=='send':
            self.message('大肥鱼',decision['text'],case['now'][11:16],model=True)
        else:self.note('模拟结果：保持沉默，本轮不发送消息。',False)
        label='DeepSeek 判断' if row.get('model_called') else '程序预判（未调用模型）'
        self.note(label+'：'+decision['reason'])

    def save(self, directory, name):
        self.y+=12
        self.d.line((42,self.y,1078,self.y),fill='#dbe2ec',width=1);self.y+=15
        self.text('回复逐字取自保存的 API 结果；群友、时间与场景为测试构造。',face=font(17),color='#77879b')
        self.d.text((1020,self.y),str(self.number)+'/4',font=font(17),fill='#77879b')
        path=directory/name
        self.image.crop((0,0,W,self.y+42)).save(path)
        print(str(path),flush=True)
        return str(path)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--input',required=True)
    args=parser.parse_args();base=Path(args.input)
    packet=json.loads((base/'结果/API实测原始结果.json').read_text(encoding='utf-8'))
    rows={row['scenario']['id']:row for row in packet['results']}
    fixed=json.loads((base/'日期修正复测/结果/API实测原始结果.json').read_text(encoding='utf-8'))['results'][0]
    out=base/'截图';out.mkdir(parents=True,exist_ok=True);images=[]

    p=Page('01 / 能不能接梗、跟上最近的话题',1)
    p.scenario(rows['hot_joke'],title='赛马娘：提前备战下一届')
    p.scenario(rows['latest_topic'],title='话题从抽卡转到外卖，接最近的梗')
    images.append(p.save(out,'01-接梗与话题切换.png'))

    p=Page('02 / 自然关心、隔一会儿追问进展',2)
    p.scenario(rows['care'],title='有人刚下班，临时加班到现在')
    p.scenario(rows['older_open_topic'],title='话题过去 110 分钟，还能否自然接上')
    images.append(p.save(out,'02-关心与延后接话.png'))

    p=Page('03 / 早安晚安自由发挥，不套固定句子',3)
    p.scenario(rows['morning_context'],title='07:00 早安，承接昨晚“最后一把”')
    p.scenario(rows['night_collision'],title='22:00 晚安 + 巡查合并，只说一条')
    p.scenario(fixed,title='没有可承接的前情，就简单问候')
    images.append(p.save(out,'03-早安与晚安.png'))

    p=Page('04 / 什么时候不说话，以及实测发现的问题',4)
    for key in ('resolved','everyone_left','no_human_activity','other_bot_only','missed_morning'):
        row=rows[key];case=row['scenario']
        p.section(case['title'],'虚拟时间 '+case['now'][11:16]+' · '+('DeepSeek 决定沉默' if row.get('model_called') else '程序直接跳过'))
        if key=='resolved':p.text('阿茶：“找到了，谢谢。”  小林：“嗯嗯。”',face=font(22))
        elif key=='everyone_left':p.text('阿茶：“我先睡了。”  小林：“我也撤了，明天再聊。”',face=font(22))
        elif key=='no_human_activity':p.text('最后一条真人消息在 13:20，巡查时已是 16:00。',face=font(22))
        elif key=='other_bot_only':p.text('真人最后发言在 12:50；15:50 只有签到助手发消息。',face=font(22))
        else:p.text('应在 07:00 说早安，恢复时已经 07:42。',face=font(22))
        p.note('不发言：'+row['decision']['reason'],False)
    p.section('实测暴露：周二被说成“新的一周”','第一轮原回复保留；补上明确星期信息后，重新调用 API')
    p.message('大肥鱼',rows['morning_empty']['decision']['text'],'第一轮',model=True,warning=True)
    p.text('2026-10-20 是星期二：这句“新的一周”不合适。',face=font(20),color='#9b5448')
    p.message('大肥鱼',fixed['decision']['text'],'修正后',model=True)
    images.append(p.save(out,'04-静默判断与日期修正.png'))
    summary={'scenarios':len(packet['results']),'first_round_api_calls':sum(bool(r.get('model_called')) for r in packet['results']),
             'rerun_api_calls':1,'real_qq_messages':0,'real_schedules_created':0,'images':images,
             'all_structural_checks':all(all(r.get('checks',{}).values()) for r in packet['results']),
             'quality_issue':'第一轮空前情早安在周二用了新一周表述，已补日历信息复测；原回复保留。'}
    (base/'结果/展示索引.json').write_bytes(json.dumps(summary,ensure_ascii=False,indent=2).encode('utf-8'))


if __name__=='__main__':main()
