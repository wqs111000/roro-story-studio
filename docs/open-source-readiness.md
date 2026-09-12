# 开源检查清单

## 已完成

- [x] 初始化本地 Git 仓库，默认分支为 `main`。
- [x] 隔离家庭照片、档案、草稿、批准内容、导出物和生成媒体。
- [x] 隔离服务 PID、日志和 Python 缓存。
- [x] 提供公开可复制的 profile 与服务数据示例。
- [x] 补充贡献、安全、架构和局域网文档。
- [x] 确认当前文本扫描未发现常见密钥模式。
- [x] 选择 Apache-2.0 并添加根目录许可证文本；代码与技术文档的默认授权边界已记录。
- [x] 仅保留两张明确授权的 README 文档示例图；不公开具体绘本内容。
- [x] 添加自动化测试工作流、版本号与变更日志。
- [x] 对公开分支的当前历史执行敏感信息扫描。

## 每次提交与发布前检查

```powershell
git status --short --ignored
git diff --cached --name-only
git grep -n -I -E "api[_-]?key|secret|password|token|BEGIN .*PRIVATE KEY"
git rev-list main | ForEach-Object { git grep -I -n -E "api[_-]?key|secret|password|token|BEGIN .*PRIVATE KEY" $_ -- }
```

还应人工检查文件名、EXIF、音频内容、文档中的绝对路径，以及任何能关联儿童身份的信息。

## 当前边界

Apache-2.0 只覆盖明确纳入的代码和技术文档。家庭内容不公开；仅 `LICENSE-STATUS.md` 明确列出的 README 示例图可再分发。远程仓库的公开切换、Release 创建与托管平台设置由仓库维护者执行。
