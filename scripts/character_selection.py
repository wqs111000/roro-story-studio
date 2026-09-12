"""Resolve a catalog character/form/outfit into a reference-bound storyboard entry."""
import argparse
import hashlib
import json
from pathlib import Path


def resolve_selection(root, character_id, form_id=None, outfit_id=None):
    catalog = json.loads((root / 'service/data/characters.json').read_text(encoding='utf-8'))
    character = next((c for c in catalog['characters'] if c['character_id'] == character_id), None)
    if character is None:
        raise ValueError('未知角色')
    form_id = form_id or character.get('default_form_id', character_id)
    form = character if form_id == character_id else next(
        (f for f in character.get('forms', []) if f['form_id'] == form_id), None)
    if form is None:
        raise ValueError('形态不属于该角色')
    if '已批准' not in form.get('status', '') or character.get('state_group') != 'approved':
        raise ValueError('角色或形态未批准')
    outfits = form.get('outfits', [])
    outfit_id = outfit_id or form.get('default_outfit_id', 'default')
    outfit = next((o for o in outfits if o['outfit_id'] == outfit_id), None)
    if (outfits and outfit is None) or (not outfits and outfit_id != 'default'):
        raise ValueError('穿着不属于该形态')
    selected = outfit or form
    if '已批准' not in selected.get('status', ''):
        raise ValueError('穿着未批准')
    url = selected.get('image_url', '')
    prefix = '/character-assets/'
    if not url.startswith(prefix) or Path(url[len(prefix):]).name != url[len(prefix):]:
        raise ValueError('无效图鉴参考路径')
    relative = 'service/assets/characters/' + url[len(prefix):]
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('图鉴参考文件不存在')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if selected.get('sha256') and selected['sha256'] != sha:
        raise ValueError('图鉴参考摘要不匹配')
    return {
        'character_id': character_id, 'form_id': form_id, 'outfit_id': outfit_id,
        'invariants': form.get('identity_invariants', form['appearance']),
        'costume': selected['appearance'],
        'reference': {'path': relative, 'sha256': sha, 'reviewed': False},
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('character_id')
    parser.add_argument('--form')
    parser.add_argument('--outfit')
    args = parser.parse_args()
    print(json.dumps(resolve_selection(Path(__file__).resolve().parents[1], args.character_id,
                                       args.form, args.outfit), ensure_ascii=False, indent=2))
