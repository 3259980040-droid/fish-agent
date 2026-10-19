"""Extract data literals from recorded upstream files without executing them."""
import ast
import json
from pathlib import Path
import re
import shutil

ROOT=Path(__file__).resolve().parents[1]
source=ROOT/'artifacts/context-research'
output=ROOT/'vendor/integrations/community-personas'
output.mkdir(parents=True,exist_ok=True)
rows=[]
qq=(source/'QQ-agent/src/personas.js').read_text(encoding='utf-8')
for key,name,text in re.findall(r"  (\w+): \{\s*name: '([^']+)',\s*text: `((?:\\.|[^`])*)`",qq,re.S):
    rows.append({'key':'qqagent-'+key,'name':'QQ Agent · '+name,'system_prompt':text.replace('\\`','`').replace('\\${','${'),
        'source':'https://github.com/K0nd1us/QQ-agent/blob/fdfd325e148d91845a0aeea3dfdff4cf92fc61a1/src/personas.js','license':'MIT'})
mai=ast.parse((source/'MaiBot/official_configs.py').read_text(encoding='utf-8'))
cls=next(n for n in mai.body if isinstance(n,ast.ClassDef) and n.name=='PersonalityConfig')
parts=[]
for node in cls.body:
    if isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name) and node.target.id in ('personality','behavior_style','reply_style'):
        literal=next(k.value for k in node.value.keywords if k.arg=='default')
        parts.append(node.target.id+'：'+ast.literal_eval(literal))
rows.append({'key':'maibot-default','name':'MaiBot · 默认日常风格','system_prompt':'\n\n'.join(parts),
    'source':'https://github.com/Mai-with-u/MaiBot/blob/ff0fe42500f5f7581e9655d1e78379407233207a/src/config/official_configs.py','license':'GPL-3.0'})
astr=ast.parse((source/'AstrBot/persona_mgr.py').read_text(encoding='utf-8'))
node=next(n for n in astr.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='DEFAULT_PERSONALITY' for t in n.targets))
prompt=ast.literal_eval(next(k.value for k in node.value.keywords if k.arg=='prompt'))
rows.append({'key':'astrbot-default','name':'AstrBot · 默认助手','system_prompt':prompt,
    'source':'https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/persona_mgr.py','license':'AGPL-3.0'})
for name in ('QQ-agent','MaiBot','AstrBot'):shutil.copy2(source/name/'LICENSE',output/(name+'-LICENSE'))
(output/'personas.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('Community presets:',len(rows))
