# 局域网绘本服务

## 启动与查看

```powershell
.\service\start-story-service.ps1
.\service\status-story-service.ps1
```

当前默认使用开发期简化模式：

```powershell
.\service\start-story-service.ps1 -ReviewMode Development
```

工作台提交“退回修改”或“通过并上架”时不要求家长 PIN。服务仍检查同源请求和请求令牌，候选版本、包摘要与不可变发布规则不变。

服务默认监听 `127.0.0.1:8877`。状态脚本会列出实际监听地址；以下地址仅为示例。需要局域网访问时，使用 `-HostAddress <受信任的局域网地址>` 显式启动，并先配置防火墙：

```text
Local URL: http://127.0.0.1:8877/
LAN URL: http://<LAN_HOST>:8877/
Review mode: development
```

手机、平板或其他电脑需要与服务电脑处在同一可信局域网，然后打开状态脚本显示的 `LAN URL`。局域网 IP 可能在重新连接 Wi-Fi 后变化，应以最新状态输出为准。

书架和绘本浏览保持直接可用。开发期操作会明确记录为未验证身份的 `local-development-workbench`，不会写成已验证家长决定。

当不可变 release 的发布清单包含多条旁白音轨时，播放器和工作台会显示声音选择器。新候选为每个声音记录独立逐页时间轴：切换声音时保留当前页及页内进度，自动翻页和手动跳页使用该声音的真实页段边界。只有缺少时间轴的旧内容才按整轨比例兼容估算；只有单条音轨时选择器自动隐藏。

主线稳定后如需恢复家长验证，先停止服务，再运行：

```powershell
.\service\start-story-service.ps1 -ReviewMode Pin
```

此时状态脚本会显示 6 位家长 PIN。PIN 保存于 `service/.runtime/parent-pin.txt`，不进入 Git；服务重启后浏览器需要重新验证。

## Windows 防火墙

首次使用时，在管理员 PowerShell 中运行：

```powershell
.\service\enable-lan-firewall.ps1
```

该脚本只开放 TCP 8877，并把远端地址限制为 `LocalSubnet`。开发模式没有家长身份验证，仅保留同源和 CSRF 防护，因此服务仍只面向可信家庭局域网，不应暴露到公网，也不应配置路由器端口转发。

## 停止服务

```powershell
.\service\stop-story-service.ps1
```

PID 与日志保存在 `service/.runtime/`，Python 缓存与运行态文件不属于项目内容。
