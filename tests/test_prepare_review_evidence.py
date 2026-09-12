import importlib.util
import copy
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('prepare_candidate', Path(__file__).resolve().parents[1] / 'scripts/prepare-review-candidate.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReviewEvidenceTests(unittest.TestCase):
    def test_review_binds_page_positions_not_only_file_membership(self):
        workspace = Path(__file__).resolve().parents[1]
        root = workspace / 'drafts' / 'example'
        assets = {'cover': 'images/cover.png', 'pages': ['images/page-01.png', 'images/page-02.png']}
        review = {'pages': [{'page': page, 'image': f'drafts/example/{path}'}
                            for page, path in enumerate([assets['cover'], *assets['pages']])]}
        module.validate_selected_images(workspace, root, assets, review)
        # Record order is irrelevant; the page-to-image relationship is not.
        reordered = {'pages': list(reversed(review['pages']))}
        module.validate_selected_images(workspace, root, assets, reordered)
        for left, right in ((0, 1), (1, 2)):
            swapped = copy.deepcopy(review)
            rows = swapped['pages']
            rows[left]['image'], rows[right]['image'] = rows[right]['image'], rows[left]['image']
            with self.assertRaises(ValueError):
                module.validate_selected_images(workspace, root, assets, swapped)
        for rows in (review['pages'][:-1], review['pages'] + [review['pages'][0]]):
            with self.assertRaises(ValueError):
                module.validate_selected_images(workspace, root, assets, {'pages': rows})

    def test_missing_or_stale_evidence_cannot_be_packaged_as_passed(self):
        candidate = {'package_digest': 'sha256:current'}
        for evidence in ({}, {'package_digest': 'sha256:old', 'result': 'passed'},
                         {'package_digest': 'sha256:current', 'result': 'passed', 'reviewer': 'codex', 'reviewed_at': 'today', 'checks': []}):
            with self.assertRaises(ValueError):
                module.validate_evidence(candidate, evidence)

    def test_every_check_requires_actual_observation(self):
        ids = ['content', 'audible_speakers', 'character', 'scene', 'full_bleed', 'contact_sheet', 'audio', 'cover_title', 'page_timeline']
        evidence = {'package_digest': 'sha256:current', 'result': 'passed', 'reviewer': 'codex', 'reviewed_at': 'today',
                    'checks': [{'id': key, 'status': 'passed', 'evidence': 'Recorded observation'} for key in ids]}
        module.validate_evidence({'package_digest': 'sha256:current'}, evidence)
        evidence['checks'][0]['evidence'] = ''
        with self.assertRaises(ValueError):
            module.validate_evidence({'package_digest': 'sha256:current'}, evidence)
