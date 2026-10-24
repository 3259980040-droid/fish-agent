"""Only the new expression selection and actual retrieval accounting."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'harness'),str(ROOT/'vendor/python')]
from group_language import GroupLanguage
from conversation_style import repetition_context
from language_workbench import LanguageWorkbench
from storage import database


class ExpressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=GroupLanguage(self.root)

    def learn(self,kind,trigger,content,source='m1'):
        snapshot={'chat_id':'private:12345678','messages':[{'message_id':source,'sender':{'id':'12345678'},'text':'我这叫提前备战下一届'}]}
        self.store.learn(snapshot,[{'message_id':source,'kind':kind,'trigger':trigger,'content':content}],'12345678')

    def test_semantic_situation_is_selected_without_old_topic_or_other_windows(self):
        self.learn('expression','游戏输了后的自我调侃','把失败说成提前准备')
        self.learn('alias','老吴','会话里的临时称呼','m2')
        with patch('memory_engine.text_similarities',return_value=[.73]):
            rows=self.store.select('private:12345678','','跑了个第二，提前备战下一届','刚才大家在叫老吴')
        self.assertEqual([r['kind'] for r in rows],['expression'])
        self.assertEqual(self.store.select('private:12345678','another','老吴'),[])

    def test_read_is_recorded_once_instead_of_counting_each_write_as_a_read(self):
        self.learn('alias','杏目','本会话称谓')
        entry=self.store.items('private:12345678','')[0]
        def usage():
            with database(self.store.path) as db:audits=[dict(r) for r in db.execute('SELECT * FROM audits')]
            data={'group':[{'id':entry['id'],'content':entry['content'],'trigger':entry['trigger'],'evidence':entry['evidence'],'source':{'chat':'private:12345678'}}],
                'audits':audits,'corrections':set()}
            with patch.object(LanguageWorkbench,'_reply_texts',return_value={}):
                LanguageWorkbench(self.root)._usage(data)
            return data['group'][0]['usage']
        self.assertEqual(usage()['retrieved'],0)
        self.store.context('private:12345678','','杏目')
        self.assertEqual(usage()['retrieved'],1)
        self.assertEqual(usage()['extracted'],1)

    def test_repetition_hint_depends_on_recent_actual_replies_and_task_intent(self):
        replies=['鱼要吃白饭','哦鲸鲸，鱼累了']
        self.assertIn('不再额外拐回',repetition_context('抓到你了',replies))
        self.assertEqual(repetition_context('认真解释一下耐力',replies),'')
        self.assertEqual(repetition_context('鱼今晚吃什么',replies),'')
        self.assertEqual(repetition_context('抓到你了',['不可能']), '')


if __name__=='__main__':unittest.main()
