# 制作管线与操作入口

更新：2026-09-05。本文说明当前源码的制作路径；不代表运行服务已更新，也不记录会过期的书籍数量。质量标准见 [绘本制作流程](PICTURE_BOOK_WORKFLOW.md)，经验见 [制作经验](PRODUCTION_LESSONS.md)。

## 一条主线

```text
真实素材 / 原创想法 → 完整故事 → 工作台加入制作
    → storyboard 分镜与引用 → 代表页 → 其余插图
    → 联系表检查 → accept / text-adapt / repair
    → 逐页旁白与各音色时间轴 → 内部看图、试听
    → inspect 选材与摘要 → 内部证据 → 锁定候选
    → 工作台最终审核 → 通过并上架 → 本地不可变 release
                        └ 退回修改 → drafts 新修订
```

Codex/ChatGPT 客户端负责创作与内部审核。网页保存选择、展示进度、预览和接受最终决定。“加入制作”不会启动后台生图或配音；生成超时后应检查是否已有实际文件，再继续缺失步骤。日常故事只用家长提供的事实。

## 文件职责与阶段完成条件

| 阶段 | 权威输入与输出 | 完成条件 |
| --- | --- | --- |
| 故事 | `inbox/` → `drafts/<story-id>/story.json`、`story.md` | 封面加完整 6 页正文；真实事实与虚构改编分清；一个成长主题 |
| 分镜 | `storyboard.json` 的 `visual_production`、`cover`、`shots[]` | 已批准角色引用、服装、故事不变量、场景状态、尺度关系齐全 |
| 插图 | 分镜派生提示词 → `images/` | 代表页合格后建立锚点；每张母版为无字全画幅场景 |
| 视觉复核 | `review.md`、`visual-review.json` | 封面页 0 和全部正文逐页记录结论、图像哈希、输入摘要、次数与理由 |
| 旁白 | `narration.json` → `audio/` 音轨及 `.sync.json` | 对话听得出说话者；每个提供的音色都有封面标题和准确逐页边界 |
| 内部审核 | `internal-review.json` | 真实检查记录绑定当前包摘要；没有待修硬伤；实际试听完成 |
| 锁定候选 | `review/candidate-manifest.json`、`review/ai-review.json` | 文件范围、页码对应、参考和证据通过校验；打包不等于上架 |
| 本地上架 | 工作台决定 → `approved/<id>/releases/<revision>/`、`current.json` | 当前版本与摘要绑定的决定已持久化；旧 release 保留 |
| 导出 | 当前明确版本 → PDF / 外部内容包 | 单独请求后生成和验证；本地上架不自动触发 |

视觉只维护一份分镜。`visual_plan.py` 输出是派生材料；将编译提示词同步回对应 shot 的 `prompt`，不要另写一份独立视觉计划。引用中的路径必须实际作为图片附件传入生图工具。

## 单本操作示例

从项目根目录执行，先把占位故事 ID 改成目标目录。以下生成和锁定命令仅用于已授权制作的单本故事，日常维护不执行批量重制。

```powershell
$storyId = 'YYYY-MM-DD-story-slug'
$revisionPrefix = 'review-YYYYMMDD-v1'
python -X utf8 scripts/visual_plan.py "drafts/$storyId/storyboard.json" --page 1
```

代表页初次建立时可无自身锚点，角色参考仍须已批准并记录哈希。后续同地点页面引用已接受的场景锚点；换地点重新建立场景。封面按页码 0 编译。冻结分镜后再记录最终 `input_digest`：当前工具将整份视觉计划纳入摘要，任何计划变化都会使旧输入绑定失效；检查受影响的关系后更新记录，未变化的图不因此自动重画或重复全图分析。

完成实际看图并记录 `visual-review.json` 后验证：

```powershell
python -X utf8 scripts/visual_plan.py "drafts/$storyId/storyboard.json" --review "drafts/$storyId/visual-review.json"
python -X utf8 scripts/render-higgs-narration.py $storyId --voices lady roro-01 wqs --page-synchronized --output-tag candidate-v1
```

音频命令需要可用的本地 TTS 服务及配置。沿用相同输出 tag 和音色可复用未变的标题/页片段。确需重做第 3 页时加 `--refresh-pages 3`；它会重拼整轨及时间轴。先同步正文、Markdown、旁白、分镜和审核说明，再处理受影响页的所有提供音色。不要常规使用 `--force` 或 `--all-drafts`。

```powershell
python -X utf8 scripts/prepare-review-candidate.py $storyId --revision-prefix $revisionPrefix --inspect
```

`--inspect` 不写候选、不产生通过记录。它按现有命名规则选择图片和声音：页图优先高版本，封面选择 `cover*.png`，音轨按声音优先级及修改时间选择；**最新文件不等于已接受文件**。核对输出的每个路径、逐音色附加轨道和同步文件，发现误选先纠正候选素材组织，再检查摘要。正式发布阶段只读取显式 manifest，不重新猜测文件。

实际审核后在草稿根目录写 `internal-review.json`，包含 `package_digest`、`result`、`reviewer`、`reviewed_at` 和 `checks`。必需检查 ID：`content`、`audible_speakers`、`character`、`scene`、`full_bleed`、`contact_sheet`、`audio`、`cover_title`、`page_timeline`；每项须有真实 `evidence` 与 `status: passed`。不能把这份字段清单当作已经完成的检查。

```powershell
python -X utf8 scripts/prepare-review-candidate.py $storyId --revision-prefix $revisionPrefix
```

两次命令使用相同故事顺序和 revision prefix；单本会生成 `<prefix>-001`，版本也是包摘要的一部分。期间改变素材或版本必须重新 inspect。正式打包额外验证视觉记录、哈希与每页选材对应；成功后到 [工作台](http://127.0.0.1:8877/workbench) 审核。开发模式记录未验证身份的本地动作，PIN 模式记录验证后的执行者；两者不能混称为家长身份验证。

## 返修时从哪里恢复

| 变化 / 故障 | 恢复位置 |
| --- | --- |
| 非关键图文差异，画面合格 | `text-adapt`，同步五份文字来源，只处理受影响页音频并重拼时间轴 |
| 手部、身份、安全、底栏、关键剧情或连续性错误 | `repair`，回到该页构图和参考；记录具名问题与尝试次数 |
| 角色参考、场景状态或分镜变化 | 更新分镜，检查依赖关系，更新输入绑定；不能直接复制旧通过结论 |
| 选错版本、页面或封面互换 | 修正选材及逐页证据；文件集合相同也不能通过 |
| 审核后内容改变，候选 stale | 新 inspect → 对应复核 → 新候选；不修改旧摘要绕过校验 |
| 发布版本冲突 | 保留原 release，创建新候选版本并重新完成审核绑定 |
| 已上架版本出现问题 | `drafts/` 制作指定范围的新候选；旧 release 不覆盖 |
| 服务未显示新结果 | 检查服务实际目录/容器来源、健康状态与缓存；源码修改不等于部署 |

## 维护与验证入口

`scripts/visual_plan.py` 管分镜引用和视觉证据；`scripts/prepare-review-candidate.py` 管选材和内审打包；`scripts/render-higgs-narration.py` 与 `service/narration_alignment.py` 管音频；`service/review_store.py` 管摘要、审核状态和发布；`service/story_server.py`、`service/ui/` 管网页与接口。NAS 同步与展示部署另见 [NAS 文档](NAS_DEPLOYMENT_MVP.md)，不能把同步当作审核。

```powershell
python -X utf8 -m unittest discover -s tests -p 'test_prepare_review_evidence.py' -v
python -X utf8 -m unittest discover -s tests -p 'test_visual_plan.py' -v
python -X utf8 -m unittest discover -s tests -q
git diff --check
```

自动测试验证程序约束。实际看图、试听、浏览器验收与运行部署分别留证；任何一项不能代替另一项。
