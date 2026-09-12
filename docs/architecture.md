# 项目架构

Roro Story Studio 由四层组成：

1. **创作规范层**：`AGENTS.md`、`skills/`、`templates/` 和 `universe/` 约束故事结构、年龄适配、角色连续性与审核边界。
2. **本地内容层**：`inbox/`、`drafts/`、`approved/` 和 `exports/` 保存家庭输入、草稿、批准包和导出物，默认不进入 Git。
3. **构建层**：`scripts/` 编译分镜参考、验证视觉证据、合成逐页音频、锁定候选，并按请求生成 PDF 等派生成品。具体职责见 [制作管线](PRODUCTION_PIPELINE.md)。
4. **服务层**：`service/story_server.py` 使用标准库 HTTP 服务提供书架、角色图鉴、审片工作台、播放器和 JSON 接口；缩略图及 PDF 使用 Pillow、ReportLab。

## 服务接口

| 路径 | 用途 |
|---|---|
| `/health` | 服务健康状态 |
| `/api/stories` | 已批准并可播放的故事目录 |
| `/api/stories/<id>` | 单本绘本页面、资源和各音色时间轴；旧内容可兼容估算 |
| `/api/characters` | 本地角色目录及作品关联 |
| `/api/studio/stories` | 草稿、候选版本与审核状态列表 |
| `/api/studio/review-queue` | 已通过 Codex 内部审核、等待家长决定的候选列表 |
| `/api/studio/auth/status` | 家长审核会话状态 |
| `POST /api/studio/auth/login` | 使用本地 6 位家长 PIN 建立写会话 |
| `POST /api/studio/stories/<id>/decision` | 持久化退回或通过决定；通过时原子发布到书架 |
| `/` | 绘本书架 |
| `/workbench` | 可写家长审片工作台；支持退回修改和通过并上架 |
| `/characters` | 角色图鉴 |
| `/player/<id>` | 通用响应式有声播放器 |

## 信任边界

- 工作台决定绑定候选版本与 SHA-256 包摘要，并持久写入草稿审核记录。
- `approved/` 只接受工作台绑定准确候选的“通过并上架”；发布版本不可变，由 `current.json` 指向当前书架版本。
- 默认开发模式免 PIN，但仍校验同源、会话与 CSRF Token，并记录 `identity_verified: false`；可选 PIN 模式要求家长身份验证。运行时凭据不进入 Git。
- 家庭内容与公开源码通过 `.gitignore` 和示例配置分离。
