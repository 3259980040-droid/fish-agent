"""Source matching, community priors and uncertainty in character lookup."""
import unittest
from vision_identification import DEFAULTS,rank_candidates


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.cfg={**DEFAULTS,'weights':{'hair':3,'eyes':2,'head_accessory':3}}
        self.features={'hair':'红发','eyes':'红瞳','head_accessory':'金色头饰'}
        self.sources=[{'title':'甲角色资料','url':'https://a.example/甲','text':'甲角色是红发红瞳，佩戴金色头饰'},
            {'title':'甲角色外观','url':'https://b.example/甲','text':'甲角色的头发为红发，双眼为红瞳，佩戴金色头饰'}]

    def candidate(self,name='甲角色',community=True):
        return {'name':name,'community_match':community,'checks':{
            'hair':{'relation':'match','url':self.sources[0]['url'],'quote':'甲角色是红发红瞳'},
            'eyes':{'relation':'match','url':self.sources[1]['url'],'quote':'双眼为红瞳'},
            'head_accessory':{'relation':'match','url':self.sources[1]['url'],'quote':'佩戴金色头饰'}}}

    def test_two_sources_and_matching_features_support_candidate(self):
        result=rank_candidates([self.candidate()],self.features,self.sources,self.cfg,'游戏群')
        self.assertEqual(result['status'],'source_supported_candidate')
        self.assertEqual(result['candidates'][0]['source_domains'],2)

    def test_invented_quote_and_other_character_page_do_not_support_identity(self):
        wrong=self.candidate('乙角色');wrong['checks']['hair']['quote']='根本不存在的原文'
        result=rank_candidates([wrong],self.features,self.sources,self.cfg,'游戏群')
        self.assertEqual(result['status'],'needs_confirmation')
        self.assertEqual(result['candidates'][0]['match_score'],0)

    def test_group_bias_cannot_override_conflicting_visual_features(self):
        good=self.candidate(community=False);bad=self.candidate(community=True)
        bad['checks']={k:{**v,'relation':'conflict'} for k,v in bad['checks'].items()}
        result=rank_candidates([bad,good],self.features,self.sources,self.cfg,'游戏群')
        self.assertEqual(result['candidates'][0]['match_score'],88.9)
        self.assertEqual(result['candidates'][1]['match_score'],0)

    def test_one_source_does_not_count_as_cross_check(self):
        result=rank_candidates([self.candidate()],self.features,self.sources[:1],self.cfg,'游戏群')
        self.assertEqual(result['status'],'needs_confirmation')


if __name__=='__main__':unittest.main()
