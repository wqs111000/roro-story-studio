import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('visual_plan', Path(__file__).resolve().parents[1] / 'scripts/visual_plan.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class VisualPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'ref.png').write_bytes(b'reference')
        self.ref = {**module.file_record(self.root, 'ref.png'), 'reviewed': True}
        self.plan = {'style': 'pencil', 'story_invariants': 'kindness', 'scale_relationship': 'adult taller',
                     'scale_anchor_page': 1, 'scale_anchor': self.ref,
                     'characters': {'child': {'invariants': 'short hair', 'costume': 'blue', 'reference': self.ref}},
                     'scenes': {'room': {'anchor_page': 1, 'anchor': self.ref, 'layout': 'window left'}},
                     'pages': [{'page': i, 'scene': 'room', 'characters': ['child'], 'composition': 'medium',
                                'action': 'sit', 'state': 'night', 'allowed_changes': 'pose'} for i in (1, 2)]}

    def review(self):
        return {'pages': [{'page': i, 'input_digest': module.compile_page(self.root, self.plan, i)['input_digest'],
                           'image': 'ref.png', 'image_sha256': self.ref['sha256'], 'verdict': 'accept',
                           'reviewer': 'codex', 'reason': 'visually inspected', 'attempts': 1} for i in (1, 2)]}

    def test_later_page_requires_anchor_but_first_can_bootstrap(self):
        self.plan['scenes']['room']['anchor'] = None
        module.compile_page(self.root, self.plan, 1)
        with self.assertRaises(ValueError):
            module.compile_page(self.root, self.plan, 2)

    def test_changed_reference_fails(self):
        (self.root / 'ref.png').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            module.compile_page(self.root, self.plan, 1)

    def test_new_location_can_establish_its_own_anchor(self):
        self.plan['scenes']['garden'] = {'anchor_page': 2, 'anchor': None, 'layout': 'tree beside path'}
        self.plan['pages'][1]['scene'] = 'garden'
        packet = module.compile_page(self.root, self.plan, 2)
        self.assertIn('tree beside path', packet['prompt'])
        self.assertNotIn('window left', packet['prompt'])
        self.assertFalse(any(ref['role'] == '本场景固定锚点' for ref in packet['references']))

    def test_plot_driven_change_in_same_location_is_allowed(self):
        self.plan['pages'][1]['state'] = 'morning; lamp off; clean bedding'
        self.plan['pages'][1]['allowed_changes'] = 'daylight and changed bedding after waking'
        packet = module.compile_page(self.root, self.plan, 2)
        self.assertIn('morning; lamp off; clean bedding', packet['prompt'])
        self.assertIn('window left', packet['prompt'])

    def test_changed_story_state_invalidates_review(self):
        review = self.review()
        self.plan['pages'][1]['state'] = 'morning'
        with self.assertRaises(ValueError):
            module.validate_review(self.root, self.plan, review)

    def test_incomplete_review_or_repair_cannot_pass(self):
        review = self.review()
        self.assertEqual(len(module.validate_review(self.root, self.plan, review)), 2)
        for mutate in (lambda r: r['pages'].pop(), lambda r: r['pages'][0].update(verdict='repair'),
                       lambda r: r['pages'][0].update(attempts=4),
                       lambda r: r['pages'][0].update(verdict='text-adapt', text_audio_synced=False)):
            broken = copy.deepcopy(review)
            mutate(broken)
            with self.assertRaises(ValueError):
                module.validate_review(self.root, self.plan, broken)

    def test_outside_workspace_rejected(self):
        with self.assertRaises(ValueError):
            module.file_record(self.root, '../outside.png')

    def test_existing_storyboard_is_the_authoring_source(self):
        storyboard = {'visual_production': {k: v for k, v in self.plan.items() if k != 'pages'},
                      'shots': self.plan['pages'],
                      'cover': {**self.plan['pages'][0], 'page': 0}}
        storyboard['shots'][0]['prompt'] = 'old generated prompt'
        normalized = module.plan_from_storyboard(storyboard)
        self.assertEqual([row['page'] for row in normalized['pages']], [0, 1, 2])
        self.assertNotIn('prompt', normalized['pages'][1])
        packet = module.compile_page(self.root, normalized, 1)
        storyboard['shots'][0]['prompt'] = packet['prompt']
        self.assertEqual(module.plan_from_storyboard(storyboard), normalized)

    def test_page_safety_constraints_survive_compilation(self):
        self.plan['pages'][0]['negative_constraints'] = ['no unsafe action']
        self.assertIn('no unsafe action', module.compile_page(self.root, self.plan, 1)['prompt'])
