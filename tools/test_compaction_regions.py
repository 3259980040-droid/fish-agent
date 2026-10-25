"""01-04 follow-up: a compaction range belongs to exactly one original store."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness'), str(ROOT / 'vendor/python')]

from fish_agent.experience.compaction import select_region, select_regions


def node(source, conversation, ident, *, bundle='', role='user', tokens=1000):
    return {'source': source, 'conversation': conversation, 'id': ident, 'bundle': bundle,
            'layer': 1, 'role': role, 'tokens': tokens, 'text': 'm%d' % ident}


class CompactionRegionTests(unittest.TestCase):
    def test_a_region_never_spans_two_stores(self):
        surface = [node('desktop-chat.db', 'conv-a', index) for index in range(1, 7)]
        surface += [node('qq.db', 'qq:group:1:qq', index) for index in range(1, 7)]
        regions = select_regions(surface, None, 2000)
        self.assertEqual(len(regions), 2, regions)
        by_source = {region['source']: region for region in regions}
        self.assertEqual(set(by_source), {'desktop-chat.db', 'qq.db'})
        self.assertEqual(by_source['desktop-chat.db']['conversation'], 'conv-a')
        self.assertEqual(by_source['qq.db']['conversation'], 'qq:group:1:qq')
        # Each range's `through` is an id of its own store, and the recent verbatim
        # window of each place is kept.
        self.assertEqual(by_source['desktop-chat.db']['through'], 4)
        self.assertEqual(by_source['qq.db']['through'], 4)
        self.assertEqual([item['source'] for item in by_source['qq.db']['nodes']], ['qq.db'] * 4)

    def test_the_boundary_still_keeps_bundles_whole_inside_one_store(self):
        surface = [node('qq.db', 'conv', index) for index in range(1, 7)]
        # The node just before the boundary shares a bundle with the one after it.
        surface[3]['bundle'] = 'bundle-x'
        surface[4]['bundle'] = 'bundle-x'
        region = select_region(surface, None, 2000)
        self.assertIsNotNone(region)
        self.assertEqual(region['through'], 3)  # cut moved earlier to keep the bundle whole
        self.assertEqual(len(region['nodes']), 3)

    def test_no_region_when_only_the_recent_window_exists(self):
        surface = [node('qq.db', 'conv', index) for index in range(1, 3)]
        self.assertEqual(select_regions(surface, None, 5000), [])


if __name__ == '__main__':
    unittest.main()
