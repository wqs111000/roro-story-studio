# 贡献指南

项目采用 Apache-2.0 作为代码、脚本、配置、Schema 和技术文档的默认许可证。提交前请确认你有权按此许可证贡献内容；素材边界详见 [许可证状态](LICENSE-STATUS.md)。

## 开发原则

- 新故事、角色、插图和音频遵循 `AGENTS.md` 与故事 Skill。
- 家庭资料、真实照片、录音、草稿和批准包不得提交。
- 不把文件生成、自动测试或只读 UI 状态解释为家长批准。
- 不复制受版权保护的角色、文本、世界观、标志性桥段或在世艺术家的独特画风。
- 服务默认仅监听本机回环地址，不提供公网认证能力；需要局域网访问时，必须显式配置监听地址并启用防火墙限制。
- 提交代码或技术文档即表示提交者有权将其置于 Apache-2.0 下；不要提交家庭内容或无权再分发的第三方素材。

## 本地检查

制作工具、资产证据与发布约束见 [制作管线](docs/PRODUCTION_PIPELINE.md)。修改这些约束时增加能证明错误候选被拒绝的回归测试。

工作台加载、认证、派生缓存和提交边界见 [代码审核与维护经验](docs/MAINTENANCE_LESSONS.md)。详情回归需要支持 `node:test` 的 Node.js；该测试不是浏览器端到端验收。

```powershell
python -X utf8 -m unittest discover -s tests -q
node --test tests/test_workbench_detail.cjs
python -B -X utf8 -c "compile(open('service/story_server.py', encoding='utf-8').read(), 'service/story_server.py', 'exec')"
.\service\start-story-service.ps1
.\service\status-story-service.ps1
git status --short --ignored
```

提交前确认没有 `inbox/`、`drafts/`、`approved/`、家庭 profile、WAV、PDF 或真实照片进入暂存区。

## 变更范围

- 一个提交解决一个清晰问题。
- UI 变更同时验证桌面端与移动端。
- 服务变更至少验证 `/health`、`/api/stories`、书架和一个播放器。
- 不在没有明确授权时提交、推送、发布 Release 或修改远程仓库设置。
