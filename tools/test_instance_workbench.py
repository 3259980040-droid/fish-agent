from pathlib import Path
import sys, tempfile, unittest, json, sqlite3
from contextlib import closing
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'harness'), str(ROOT/'tools')]
import dependencies
from workbench import Workbench

class InitialInstanceTests(unittest.TestCase):
    def test_original_files_and_explicit_workbench_target_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'harness').mkdir()
            (root/'harness/agents.json').write_text('{"agents":[{"id":"qq","name":"Fish","db":"data/qq/agent.db","workspace":"workspace/qq"}]}', encoding='utf-8')
            (root/'workspace').mkdir()
            (root/'workspace/article.txt').write_text('原来的文章', encoding='utf-8')
            bench = Workbench(root)
            listed = bench.dispatch('instances.list', {})
            self.assertEqual(listed['initial_instance_id'], 'main')
            self.assertEqual([row['id'] for row in listed['instances']], ['main'])
            self.assertEqual(bench.dispatch('workspace.read', {'instance_id':'main', 'path':'article.txt'})['text'], ['原来的文章'])
            self.assertFalse((root/'workspace/article.txt').exists())
            with self.assertRaises(ValueError):
                bench.dispatch('workspace.list', {'instance_id':'missing'})
            reopened = Workbench(root)
            self.assertEqual(reopened.dispatch('instances.list', {})['instances'][0]['id'], 'main')
            self.assertEqual(reopened.dispatch('workspace.read', {'instance_id':'main', 'path':'article.txt'})['text'], ['原来的文章'])


    def test_migrated_history_persona_and_tool_result_share_one_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'harness').mkdir()
            (root/'harness/agents.json').write_text(json.dumps({'agents':[{'id':'qq','name':'Fish','db':'data/qq/agent.db','workspace':'qq-work','persona':'harness/agents/qq.persona.md'}]}), encoding='utf-8')
            (root/'harness/integration.json').write_text('{"python_executable":"shared-runtime","qzone_enabled":true}', encoding='utf-8')
            (root/'qq-work').mkdir()
            (root/'qq-work/AGENTS.md').write_text('程序模板', encoding='utf-8')
            (root/'qq-work/article.md').write_text('工作成果', encoding='utf-8')
            media = root/'data/qq/media/original.txt'
            media.parent.mkdir(parents=True)
            media.write_text('原件', encoding='utf-8')
            original_text = '原话与路径不改：' + str(media)
            with closing(sqlite3.connect(root/'data/qq/desktop-chat.db')) as db:
                db.executescript('CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT,session_id TEXT,updated_at TEXT); CREATE TABLE messages(id INTEGER PRIMARY KEY,conversation_id TEXT,direction TEXT,text TEXT,segments TEXT,status TEXT,error TEXT,created_at TEXT);')
                db.execute('INSERT INTO conversations VALUES(?,?,?,?)',('original','原对话',None,'2026-10-26'))
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)',(41,'original','inbound',original_text,json.dumps([{'path':str(media)}]),'done','','2026-10-26'))
                db.commit()
            bench = Workbench(root)
            listed = bench.dispatch('instances.list', {})
            self.assertEqual(listed['instances'][0]['id'], 'main')
            history = bench.dispatch('chat.snapshot', {'instance_id':'main','id':'original'})
            self.assertEqual(str(history['messages'][0]['id']), '41')
            self.assertEqual(history['messages'][0]['text'], original_text)
            self.assertEqual(Path(history['messages'][0]['segments'][0]['path']).read_text(encoding='utf-8'), '原件')
            saved = bench.dispatch('personas.save', {'instance_id':'main','name':'原人格','body':'自己的性格'})
            bench.dispatch('personas.apply', {'instance_id':'main','revision':saved['id']})
            self.assertEqual(Workbench(root).dispatch('personas.current', {'instance_id':'main'})['body'], '自己的性格')
            from fish_agent import get_runtime, reset_runtime
            runtime = get_runtime(root)
            self.assertIs(runtime, bench.runtime())
            self.assertEqual(runtime.client.inbound('original', 41)['text'], original_text)
            from tool_host import ToolHost
            host = ToolHost(root)
            host.bind('qq','original-tool','owner',desktop=True)
            self.assertEqual(host.identity('qq','original-tool')['instance_id'], 'main')
            result = host.execute('qq','original-tool','fish_workspace',{},'workspace-result')
            self.assertTrue(Path(result['files']).is_relative_to(bench.context.workspace))
            self.assertEqual((root/'qq-work/AGENTS.md').read_text(encoding='utf-8'), '程序模板')
            self.assertEqual(json.loads((root/'harness/integration.json').read_text())['python_executable'], 'shared-runtime')
            self.assertFalse((root/'data/qq/desktop-chat.db').exists())
            host.computer.interaction.close()
            reset_runtime(root)

if __name__ == '__main__': unittest.main()
