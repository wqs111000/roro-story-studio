# 绿联 NAS 绘本展示版部署

目标为 Linux amd64 NAS，目录由部署者自行选择；不要把真实主机名、共享路径或局域网地址提交到 Git。

NAS 只是展示端：仅保存书架当前上架的最终版本和当前角色图鉴；制作、审核、上下架仍在电脑进行。
不传草稿、待审核/已下架内容、制作队列、历史 release、审核记录、分享记录、旧 PDF 缓存或参考照片。

准备命令：

```powershell
.\scripts\nas\bootstrap-nas.ps1 `
  -NasRoot '<NAS_SHARED_FOLDER>\roro-story-studio' `
  -NasLinuxRoot '/volume1/docker/roro-story-studio' `
  -DesktopUrl 'http://<DESKTOP_HOST>:8877'
```

脚本读取电脑当前 `/api/stories` 的可见书架，用各书 `current.json` 选择唯一 release，
按发布 manifest 校验并复制绑定的文字、页面、全部音轨和同步文件；角色图片由当前图鉴映射选择。
复制后再次读取书架和逐文件复核，变化或缺失即留下 `INCOMPLETE` 并禁止启动。
运行环境镜像采用固定版本的 `linux/amd64` 离线包，只包含 Python、字体和 PDF 依赖；
代码位于 NAS 的 `app/service` 与 `app/scripts`，通过只读挂载运行。普通代码更新只需替换这两个目录后重启容器；
只有 Python、字体或依赖清单变化时才重建并重新导入镜像。

NAS Compose 以非 root 运行。根文件系统保持可写以兼容绿联嵌套挂载；代码只读挂载，发布内容、服务数据、角色图片和同步状态使用受限可写挂载，PDF 缓存可写。
挂载目录缺失时拒绝启动。首次部署或运行环境变化时才导入准备好的离线镜像；如果 NAS 已有相同运行环境，内容/代码更新不需要重新导入 `image.tar`。`--display-only` 隐藏工作台和分享按钮，禁用工作台、候选预览及非同步写 API。

首次准备包可先放在 `staging/<版本>/` 做校验；正式部署完成后，应将已验证的 `app`、`data`、`service-data`、`character-assets`、`sync-state`、`docker-compose.yaml` 和 `.env` 切换到部署者配置的主目录，并从主目录启动项目。不要让运行中的项目长期指向 staging 目录。
在绿联“镜像”页面导入 `image.tar` 后，在“项目”页面选择该目录并导入
`compose.yaml` 和包内 `.env`；核对 `PUID=1000`、`PGID=10` 与 `RORO_LISTEN_ADDRESS`。脚本的 `-ListenAddress` 写入该地址；未配置时 Compose 仅绑定回环地址。
首次部署仍使用完整展示包。内容同步模块启用后，NAS 书架可通过“检查更新”从
`-DesktopUrl` 指定的桌面端按版本增量同步新增、修改、恢复和下架内容；同步完成后不需要重启容器。
同步模块只允许安装桌面端已发布且摘要校验通过的展示内容，不开放 NAS 制作、审核或上架写入。代码更新仍需替换代码目录并重启，Python、字体或依赖变化才重建镜像。NAS 仍是展示副本，
制作、审核和上架权威只在桌面端；不要双向同步电脑与 NAS 的业务目录。

## 现场部署经验

- 绿联 Docker 对绑定具体 NAS IP 的端口写法兼容性不稳定；使用 `8877:8877`，不要依赖 `${RORO_LISTEN_ADDRESS}:8877:8877`。
- `/app/service` 是只读代码挂载时，任何嵌套挂载点必须在宿主机预先创建。角色目录必须存在：`app/service/assets/characters/`，否则容器会在只读 overlay 中创建挂载点失败。
- 桌面端角色资源实际位于 `/app/service/assets/characters`，不能从展示目录父级推导成 `/data/service/assets/characters`。同步接口必须基于 `Path(__file__).resolve().parent` 定位应用资源。
- 内容同步失败时，先检查 `/api/sync/status` 和桌面端 `/api/sync/character-assets/<name>`；角色资源 404 通常是路径或挂载问题，不是绘本 release 本身损坏。
- 代码/内容切换前先停止旧项目，并在主目录内保留可回滚备份；staging 只是校验和中转位置，不是最终运行目录。
