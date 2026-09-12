# 形态与穿着选择

图鉴模型是人物 → 形态 → 穿着。`character_id` 锁定人物；基础形态沿用 `character_id` 作为 `form_id`，其他形态使用 `forms[].form_id`。基础形态的 `outfits` 存在人物根节点，其他形态的 `outfits` 存在对应 form 节点，不能跨形态借用衣服参考。这是兼容旧图鉴的存储方式，不是把服装注册为新人物。

每套穿着包含 `outfit_id`、中文名称、批准状态、服装描述、参考图 URL、可选 SHA-256 与适用 `contexts`。身份不变项放在形态的 `identity_invariants`。未配置穿着的历史形态保留 `default` 原始形象（包括不穿衣服的恐龙/青蛙）；有 outfits 时以 `default_outfit_id` 为默认。

制作时先尊重指定的形态/衣服；否则依据情节选形态，再在该形态内依据活动与氛围选衣服。佑佑人类形态：普通日常用日常蓝衣，森林童话可用森林小王子，魔法梦境可用见习魔法师，户外探索可用故事探险家，居家睡前可用云朵月亮睡衣。服装不是孩子现实偏好的证据。Roro 仍默认暖阳形态。

解析示例：

```powershell
python scripts/character_selection.py youyou --form youyou --outfit forest-little-prince
python scripts/character_selection.py roro
```

将返回对象合入现有 `storyboard.json` 的角色项，补充 `selection_reason`，实际看图后设置 `reference.reviewed: true`。解析器验证目录归属和文件摘要，但不会替代看图。编译器验证人物/形态/穿着归属、批准状态、参考路径与摘要，以及身份和服装文字，拒绝选魔法师却附蓝T恤参考的情况。

整本默认固定组合，服饰与身份约束分别记录。剧情明确换装时，在同一 `visual_production.characters` 下建立两个角色绑定键（如 `youyou-day` 和 `youyou-night`），两者 `character_id` 相同但 outfit 不同，每页 `characters` 引用实际穿着的绑定键；记录换装页与理由，不凭空改变体型。封面也绑定所展示的组合。道具是否携带由情节决定。

新穿着缺少合格参考时先制作与检查设定图，不用文字路径冒充附件。用户已授权的选择沿用；未获批准的候选不自动标为已批准。修改 skill 和图鉴不迁移历史分镜或重发旧书。旧分镜不含选择字段时继续使用原绑定。
