import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('visual_plan', ROOT / 'scripts/visual_plan.py')
visual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visual)
spec = importlib.util.spec_from_file_location('selection', ROOT / 'scripts/character_selection.py')
selection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selection)


class CharacterSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'service/data').mkdir(parents=True)
        (self.root / 'service/assets/characters').mkdir(parents=True)
        (self.root / 'service/assets/characters/blue.png').write_bytes(b'blue')
        (self.root / 'service/assets/characters/star.png').write_bytes(b'star')
        self.character = {'character_id': 'child', 'status': '已批准', 'state_group': 'approved',
                          'appearance': 'short hair', 'identity_invariants': 'short hair',
                          'default_outfit_id': 'blue', 'outfits': [
                              {'outfit_id': 'blue', 'status': '已批准', 'appearance': 'blue shirt', 'image_url': '/character-assets/blue.png'},
                              {'outfit_id': 'star', 'status': '已批准', 'appearance': 'star cloak', 'image_url': '/character-assets/star.png'}]}
        self.save()

    def save(self):
        (self.root / 'service/data/characters.json').write_text(json.dumps({'characters': [self.character]}), encoding='utf-8')

    def test_selection_and_invalid_combinations(self):
        self.assertEqual(selection.resolve_selection(self.root, 'child')['outfit_id'], 'blue')
        chosen = selection.resolve_selection(self.root, 'child', 'child', 'star')
        self.assertEqual(chosen['costume'], 'star cloak')
        self.assertFalse(chosen['reference']['reviewed'])
        for form, outfit in [('dinosaur', 'star'), ('child', 'missing')]:
            with self.assertRaises(ValueError):
                selection.resolve_selection(self.root, 'child', form, outfit)

    def test_draft_and_changed_asset_rejected(self):
        self.character['outfits'][1]['status'] = '待确认'
        self.save()
        with self.assertRaises(ValueError):
            selection.resolve_selection(self.root, 'child', 'child', 'star')
        self.character['outfits'][1].update(status='已批准', sha256='0' * 64)
        self.save()
        with self.assertRaises(ValueError):
            selection.resolve_selection(self.root, 'child', 'child', 'star')

    def test_compile_uses_selected_costume_and_rejects_wrong_reference(self):
        entry = selection.resolve_selection(self.root, 'child', 'child', 'star')
        entry['reference']['reviewed'] = True
        entry['selection_reason'] = 'dream'
        plan = {'style': 'pencil', 'story_invariants': 'kind', 'scale_relationship': 'small',
                'scale_anchor_page': 1, 'characters': {'child': entry},
                'scenes': {'room': {'anchor_page': 1, 'layout': 'window left'}},
                'pages': [{'page': 1, 'scene': 'room', 'characters': ['child'], 'action': 'sit',
                           'composition': 'medium', 'state': 'night', 'allowed_changes': 'pose'}]}
        packet = visual.compile_page(self.root, plan, 1)
        self.assertIn('star cloak', packet['prompt'])
        self.assertTrue(packet['references'][0]['path'].endswith('star.png'))
        entry['reference'] = selection.resolve_selection(self.root, 'child')['reference']
        entry['reference']['reviewed'] = True
        with self.assertRaises(ValueError):
            visual.compile_page(self.root, plan, 1)

    def test_legacy_form_keeps_default_image(self):
        self.character.pop('outfits')
        self.character.pop('default_outfit_id')
        self.character['image_url'] = '/character-assets/blue.png'
        self.save()
        self.assertEqual(selection.resolve_selection(self.root, 'child')['outfit_id'], 'default')

    def test_nested_form_outfits_and_default_form(self):
        self.character['default_form_id'] = 'frog'
        self.character['forms'] = [{'form_id': 'frog', 'status': '形态已批准',
                                    'appearance': 'green frog', 'default_outfit_id': 'rain',
                                    'outfits': [{'outfit_id': 'rain', 'status': '已批准',
                                                 'appearance': 'rain cape', 'image_url': '/character-assets/star.png'}]}]
        self.save()
        chosen = selection.resolve_selection(self.root, 'child')
        self.assertEqual((chosen['form_id'], chosen['outfit_id']), ('frog', 'rain'))
        with self.assertRaises(ValueError):
            selection.resolve_selection(self.root, 'child', 'frog', 'blue')
