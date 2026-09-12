"""Compile reference-bound image instructions; never infer visual acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
import importlib.util
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_record(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"参考文件不存在或越出工作区：{relative}")
    return {"path": path.relative_to(root.resolve()).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def reference(root, value, role):
    if not value or not value.get('reviewed') or not value.get('sha256'):
        raise ValueError(f"{role} 尚未记录人工看图确认和文件摘要")
    record = file_record(root, value['path'])
    if record['sha256'] != value['sha256']:
        raise ValueError(f"{role} 文件已变化，需要重新确认")
    return {**record, 'role': role}


def plan_from_storyboard(storyboard):
    """Read continuity from the existing storyboard, without a second authoring file."""
    production = storyboard.get('visual_production')
    if not production:
        raise ValueError('请在 storyboard.json 的 visual_production 中补充视觉设定')
    keys = ('page', 'scene', 'characters', 'composition', 'action', 'state',
            'allowed_changes', 'previous_state_reference', 'mood', 'negative_constraints')
    shots = list(storyboard['shots'])
    if storyboard.get('cover'):
        shots = [{**storyboard['cover'], 'page': 0}, *shots]
    return {**production, 'pages': [{key: shot[key] for key in keys if key in shot} for shot in shots]}


def compile_page(root, plan, page_number):
    pages = plan['pages']
    numbers = [p['page'] for p in pages]
    if len(numbers) != len(set(numbers)):
        raise ValueError('页码重复')
    page = next(p for p in pages if p['page'] == page_number)
    scene = plan['scenes'][page['scene']]
    for name in ('style', 'story_invariants', 'scale_relationship'):
        if not plan.get(name):
            raise ValueError(f'请填写 {name}')
    for name in ('action', 'composition', 'state', 'allowed_changes'):
        if not page.get(name):
            raise ValueError(f'第 {page_number} 页缺少 {name}')
    if not scene.get('layout'):
        raise ValueError('场景必须写明固定布局和摄影方向')
    refs = []
    characters = []
    for key in page['characters']:
        character = plan['characters'][key]
        if any(field in character for field in ('character_id', 'form_id', 'outfit_id')):
            if not all(character.get(field) for field in ('character_id', 'form_id', 'outfit_id', 'selection_reason')):
                raise ValueError(f'{key} 缺少形态、穿着或选择理由')
            spec = importlib.util.spec_from_file_location('character_selection', Path(__file__).with_name('character_selection.py'))
            selection_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(selection_module)
            selected = selection_module.resolve_selection(root, character['character_id'], character['form_id'], character['outfit_id'])
            for field in ('path', 'sha256'):
                if character.get('reference', {}).get(field) != selected['reference'][field]:
                    raise ValueError(f'{key} 所选形态/穿着与实际参考图不匹配')
            if character.get('costume') != selected['costume'] or character.get('invariants') != selected['invariants']:
                raise ValueError(f'{key} 图鉴设定已变化，请重新绑定并检查服装约束')
        if not character.get('invariants') or not character.get('costume'):
            raise ValueError(f'{key} 缺少身份或服装约束')
        refs.append(reference(root, character['reference'], f'角色身份：{key}'))
        characters.append(f"{key}：{character['invariants']}。本故事服装：{character['costume']}")
    if page_number != scene['anchor_page']:
        refs.append(reference(root, scene.get('anchor'), '本场景固定锚点'))
    if page_number != plan['scale_anchor_page']:
        refs.append(reference(root, plan.get('scale_anchor'), '本故事尺度和画风锚点'))
    if page.get('previous_state_reference'):
        refs.append(reference(root, page['previous_state_reference'], '道具状态补充参考'))
    prompt = '\n'.join([
        '生成原创儿童绘本单页。4:3 横版全画幅，环境自然延伸到四边；无文字、边框、横幅、字幕区、底部色带或排版占位。',
        '参考优先级：角色图锁身份；本场景锚点仅锁定剧情未改变的空间关系；尺度锚点只参考比例与画风，不复制其背景；本页状态和允许变化控制剧情变化。发生未解释的冲突时暂停，不自行折中。',
        f"统一画风：{plan['style']}", f"不可改变的故事事实：{plan['story_invariants']}",
        *characters, f"故事内比例：{plan['scale_relationship']}",
        f"本场景基准布局（仅未被剧情改变的部分延续）：{scene['layout']}", f"本页镜头：{page['composition']}",
        f"本页唯一主要动作：{page['action']}", f"本页道具、服装、光线状态：{page['state']}",
        f"本页情绪：{page.get('mood', '')}",
        f"本页禁止事项：{'；'.join(page.get('negative_constraints', []))}",
        f"允许变化：{page['allowed_changes']}。除此之外保留参考中的已锁定特征。",
        '自然遮挡可以接受；不把遮挡误画成缺肢。避免不必要的复杂手势、交叉肢体和新视角。',
    ])
    # Bind the complete plan so revisions to cross-page causality invalidate old checks too.
    packet = {'page': page_number, 'prompt': prompt, 'references': refs, 'plan_digest': digest(plan)}
    packet['input_digest'] = digest(packet)
    return packet


def validate_review(root, plan, review):
    """Verify provenance and explicit decisions, not image semantics."""
    rows = review.get('pages', [])
    if sorted(row['page'] for row in rows) != sorted(p['page'] for p in plan['pages']):
        raise ValueError('视觉审核页码不完整或重复')
    checked = []
    for row in rows:
        packet = compile_page(root, plan, row['page'])
        if row.get('input_digest') != packet['input_digest']:
            raise ValueError(f"第 {row['page']} 页参考或约束已变化，需要重新确认")
        image = file_record(root, row['image'])
        if image['sha256'] != row.get('image_sha256'):
            raise ValueError('图片已变化，旧审核无效')
        if row.get('verdict') not in ('accept', 'text-adapt') or not row.get('reason'):
            raise ValueError('仍有待修图片或缺少看图结论')
        if not row.get('reviewer'):
            raise ValueError('缺少实际看图审核者')
        if row['verdict'] == 'text-adapt' and not row.get('text_audio_synced'):
            raise ValueError('文本适配后必须同步正文和受影响页旁白')
        attempts = row.get('attempts', 0)
        if attempts < 1 or attempts > 3 or (attempts == 3 and not row.get('second_repair_reason')):
            raise ValueError('超出返修预算或未解释第二次返修')
        checked.append(image)
    return checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('storyboard', type=Path)
    parser.add_argument('--page', type=int)
    parser.add_argument('--review', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    plan = plan_from_storyboard(json.loads(args.storyboard.read_text(encoding='utf-8')))
    if args.review:
        result = {'verified_records': validate_review(root, plan, json.loads(args.review.read_text(encoding='utf-8'))),
                  'notice': '仅验证审核记录与文件绑定；不代替实际看图。'}
    elif args.page is not None:
        result = compile_page(root, plan, args.page)
    else:
        parser.error('指定 --page 编译单页，或 --review 验证整本记录')
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output = args.output.resolve()
        if not output.is_relative_to(root / 'drafts'):
            raise ValueError('制作材料只写入 drafts/')
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as handle:
            handle.write(content + '\n')
    else:
        print(content)


if __name__ == '__main__':
    main()
