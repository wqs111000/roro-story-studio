# 许可证状态

## 已选择的默认许可证

仓库中的代码、脚本、配置、Schema 和技术文档采用 Apache License 2.0，完整文本见根目录 [LICENSE](LICENSE)。其 SPDX 标识为 `Apache-2.0`。选择理由是它允许商业和非商业再分发，并提供明确的专利授权、修改声明和贡献条款；许可证本身不授予项目名称或商标使用权。

除非文件另有明确声明，首次公开发布时只将可公开的代码和技术文档纳入该许可证。建议在源文件或新文件中使用 `SPDX-License-Identifier: Apache-2.0`，但不要给私人内容补许可证头来绕过发布边界。

## 不随默认许可证公开的内容

以下内容不因仓库采用 Apache-2.0 而自动获得公开授权，也不应进入公开发布：儿童照片、录音、家庭身份资料、私人故事、角色注册表、草稿、批准包、真实生成媒体、运行日志和本地绝对路径。`inbox/`、`drafts/`、`approved/`、`exports/`、`output/` 与被忽略的本地 profile 继续按隐私边界处理。

示例故事、插图、角色设定、音频、字体、模型和第三方素材需要逐项确认权利来源。没有完成独立内容授权前，不把它们标为 Apache-2.0，也不把家庭素材改名后当作公开示例。

## 明确公开的文档示例图

以下两张 README 文档示例图由仓库维护者明确授权随仓库再分发，并采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.zh-hans)：

- `docs/assets/story-production-pipeline.png`
- `docs/assets/storybook-shelf.png`

它们仅用于说明产品工作流和界面能力，不构成可复用的故事包；除此以外，不应提交任何具体绘本、原始插画、旁白或家庭素材。

## 发布维护要求

1. 每次提交前复核暂存区、文件名和忽略规则，确保家庭资料与生成内容未进入 Git。
2. 新增依赖、字体、模型、声音或素材时，记录来源和再分发义务；不明确时不纳入仓库。
3. 合并前运行测试、敏感信息扫描和 CI；发布版本时更新 `VERSION` 与 `CHANGELOG.md`。
4. 公开仓库的可见性、Release 创建和安全报告渠道由仓库维护者在托管平台中管理。

本文件记录项目选择和边界，不是法律意见。Apache-2.0 的正式条款以 [Apache Software Foundation 的许可证文本](https://www.apache.org/licenses/LICENSE-2.0.txt)为准。
