"""Exact-image identifications backed by an actual owner correction."""
import hashlib
import json
import time
from pathlib import Path
from storage import database
from governance import Governance


class ImageKnowledge:
    def __init__(self, root):
        self.root=Path(root);self.path=self.root/'data/qq/image-knowledge.db'
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS confirmations(digest TEXT PRIMARY KEY,quote TEXT,image_message INTEGER,correction_message INTEGER,created REAL)')

    def confirm(self, path, image_message, correction_message, quote):
        with database(self.root/'data/qq/qq.db') as db:
            original=db.execute('SELECT * FROM messages WHERE id=? AND direction=\'inbound\'',(image_message,)).fetchone()
            correction=db.execute('SELECT * FROM messages WHERE id=? AND direction=\'inbound\'',(correction_message,)).fetchone()
        owner=Governance(self.root).owner_id()
        if not original or not correction or not owner or original['sender_id']!=owner or correction['sender_id']!=owner:
            raise ValueError('图片确认需对应主人真实消息')
        if original['chat_id']!=correction['chat_id'] or original['id']>=correction['id'] or not quote or quote not in correction['text']:
            raise ValueError('图片与纠正原话不匹配')
        if not any(s.get('type')=='image' for s in json.loads(original['segments'] or '[]')):
            raise ValueError('来源消息没有图片')
        digest=hashlib.sha256(Path(path).read_bytes()).hexdigest()
        with database(self.path) as db:
            db.execute('INSERT OR REPLACE INTO confirmations VALUES(?,?,?,?,?)',(digest,quote,image_message,correction_message,time.time()))
        return {'ok':True,'image_message':image_message,'correction_message':correction_message}

    def prompt(self, path):
        digest=hashlib.sha256(Path(path).read_bytes()).hexdigest()
        with database(self.path) as db:
            row=db.execute('SELECT * FROM confirmations WHERE digest=?',(digest,)).fetchone()
        if not row:return ''
        return ('[本图文件与已确认图片完全一致。主人对该图片的纠正原话，来源消息'+str(row['correction_message'])+']\n'
                +row['quote']+'\n仅以这句原话为已确认身份；不要从旧回答补入未经确认的外文名、瞳孔或设定。')
